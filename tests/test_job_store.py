"""Synthetic transaction faults, migration refusal and bounded writer reads."""

import json
import subprocess
import sys
import uuid
from dataclasses import field, make_dataclass, replace
from pathlib import Path

import pytest

from taskwatch import job_store
from taskwatch.job_watch import needs
from taskwatch.jobs import Jobs, JobState, _digest
from taskwatch.state import RecordError
from tests.test_jobs import payload

_FAULT_WRITER = r"""
import json, os, sys
from pathlib import Path
from taskwatch import job_store as store
from taskwatch.jobs import JobState, Jobs
root, data, mode = sys.argv[1:]
job = JobState(**json.loads(data))
atomic, rename, write, unlink, sync = (
    store._atomic, os.replace, store._write_all, Path.unlink, os.fsync
)
written = False

def stop():
    os._exit(73)

def atomic_fault(path, data):
    atomic(path, data)
    if mode == 'prepared' and path.name == 'jobs-pending.json':
        stop()
    if mode == 'index' and path.name == 'jobs-store.json':
        stop()

def rename_fault(src, dest):
    rename(src, dest)
    if mode == 'renamed' and Path(dest).name == 'state.json':
        stop()

def write_fault(fd, data):
    global written
    event = b'"projection"' in data and b'"sequence"' in data
    if mode == 'partial' and event:
        write(fd, data[:len(data)//2])
        os.fsync(fd)
        stop()
    write(fd, data)
    if mode == 'fsync' and event:
        written = True

def sync_fault(fd):
    global written
    if written:
        written = False
        raise OSError('synthetic fsync failure')
    sync(fd)

def unlink_fault(path, *a, **kw):
    if mode == 'committed' and path.name == 'jobs-pending.json':
        stop()
    return unlink(path, *a, **kw)

store._atomic, os.replace, store._write_all = atomic_fault, rename_fault, write_fault
Path.unlink, os.fsync = unlink_fault, sync_fault
try:
    Jobs(Path(root)).save(job, 2)
except OSError:
    stop()
raise SystemExit(9)
"""


@pytest.mark.parametrize("fault", ["prepared", "renamed", "index", "partial", "committed", "fsync"])
def test_prepared_transaction_rolls_forward_in_separate_process(tmp_path, fault):
    jobs = Jobs(tmp_path)
    original = jobs.submit(payload(tmp_path), 1)
    created = replace(original, id=uuid.uuid4().hex, receipt_key="synthetic:new", stage="user_wait")
    child = subprocess.run(
        [sys.executable, "-c", _FAULT_WRITER, str(tmp_path), json.dumps(created.to_json()), fault],
        check=False,
    )
    assert child.returncode == 73
    assert (jobs.state.dir / "jobs-pending.json").exists()
    recovered = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; from taskwatch.jobs import Jobs; "
                "j=Jobs(Path(sys.argv[1])); "
                "print(j.load()[sys.argv[2]].stage)"
            ),
            str(tmp_path),
            created.id,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert recovered.stdout.strip() == "user_wait"
    assert not (jobs.state.dir / "jobs-pending.json").exists()
    events = [
        json.loads(line)
        for line in (jobs.state.dir / "job-changes.jsonl").read_bytes().splitlines()
    ]
    assert [row["sequence"] for row in events] == [1, 2]
    assert events[-1]["job_id"] == created.id
    assert jobs.load()[created.id] == created
    assert jobs.submit({"receipt_key": created.receipt_key}, 5) == created
    code, text = needs(tmp_path, cursor=0)
    assert code == 0 and created.id in text and "user_wait" in text
    if fault == "partial":
        assert list(jobs.state.dir.glob("job-changes.jsonl.torn-*"))


def _legacy(tmp_path, count=1):
    donor = Jobs(tmp_path / "donor")
    job = donor.submit(payload(tmp_path), 1)
    jobs = Jobs(tmp_path / "root")
    jobs.state.dir.mkdir(parents=True)
    data = job.to_json()
    row = {"version": 1, "job": data, "sha256": _digest(data)}
    path = jobs.state.dir / "jobs.jsonl"
    path.write_text((json.dumps(row) + "\n") * count)
    return jobs, job, path


def test_interrupted_migration_checks_exact_staged_bytes(tmp_path, monkeypatch):
    jobs, job, legacy = _legacy(tmp_path, 3)
    original = job_store._atomic

    def interrupt(path, data):
        if path.name == "jobs-store.json":
            raise OSError("synthetic pre-marker crash")
        original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(job_store, "_atomic", interrupt)
        with pytest.raises(OSError):
            jobs.load()
    backup = legacy.read_bytes()
    assert Jobs(jobs.state.root).load()[job.id] == job
    assert legacy.read_bytes() == backup


def test_conflicting_pre_marker_snapshot_fails_closed(tmp_path):
    jobs, job, _ = _legacy(tmp_path)
    folder = jobs.folder(job)
    folder.mkdir(parents=True)
    data = job_store._envelope(replace(job, stage="done").to_json(), 1)
    (folder / "state.json").write_bytes(job_store._bytes(data))
    with pytest.raises(RecordError, match="conflicting"):
        jobs.load()
    assert not (jobs.state.dir / "jobs-store.json").exists()


def test_orphan_snapshot_is_not_adopted(tmp_path):
    jobs = Jobs(tmp_path)
    folder = jobs.state.dir / "jobs" / ("a" * 32)
    folder.mkdir(parents=True)
    (folder / "state.json").write_text("{}")
    with pytest.raises(RecordError, match="orphaned"):
        jobs.load()


@pytest.mark.parametrize("kind", ["missing", "checksum", "receipt", "unknown"])
def test_indexed_state_never_becomes_absent(tmp_path, kind):
    jobs = Jobs(tmp_path)
    job = jobs.submit(payload(tmp_path), 1)
    path = jobs.folder(job) / "state.json"
    obj = json.loads(path.read_bytes())
    if kind == "missing":
        path.unlink()
    else:
        if kind == "checksum":
            obj["revision"] += 1
        elif kind == "receipt":
            obj["job"]["receipt_key"] = "changed"
            obj["sha256"] = _digest({"revision": obj["revision"], "job": obj["job"]})
        else:
            obj["job"]["not_a_typed_field"] = True
            obj["sha256"] = _digest({"revision": obj["revision"], "job": obj["job"]})
        path.write_text(json.dumps(obj))
    with pytest.raises(RecordError):
        Jobs(tmp_path).load()


def test_storage_passes_through_new_typed_field(tmp_path, monkeypatch):
    jobs = Jobs(tmp_path)
    job = jobs.submit(payload(tmp_path), 1)
    extended = make_dataclass(
        "ExtendedJobState",
        [("synthetic_field", dict, field(default_factory=dict))],
        bases=(JobState,),
        frozen=True,
        slots=True,
    )
    from taskwatch import jobs as schema

    monkeypatch.setattr(schema, "JobState", extended)
    changed = extended(**job.to_json(), synthetic_field={"nested": ["kept", 7]})
    jobs.save(changed, 2)
    assert jobs.load()[job.id].to_json()["synthetic_field"] == {"nested": ["kept", 7]}


@pytest.mark.parametrize("obsolete", [1, 200])
def test_save_reads_only_affected_snapshot_and_unseen_event_tail(tmp_path, monkeypatch, obsolete):
    jobs, job, legacy = _legacy(tmp_path, obsolete)
    jobs.load()
    unrelated = replace(job, id=uuid.uuid4().hex, receipt_key="synthetic:other")
    jobs.save(unrelated, 2)
    jobs.save(job, 3)
    forbidden = {legacy, jobs.folder(unrelated) / "state.json"}
    opened = []
    original = Path.open

    def observed(path, *args, **kwargs):
        if path in forbidden:
            raise AssertionError(f"writer read unrelated/history file: {path.name}")
        opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed)
    jobs.save(replace(job, stage="done"), 4)
    assert jobs.folder(job) / "state.json" in opened
    event = json.loads((jobs.state.dir / "job-changes.jsonl").read_bytes().splitlines()[-1])
    assert set(event["projection"]) == {"stage", "wait_reason", "entry", "predecessors", "last_at"}
    assert "job" not in event and "prompt" not in event["projection"]


def test_concurrent_process_receipt_retry_is_one_job(tmp_path):
    submitted = payload(tmp_path)
    script = (
        "import json,sys; from pathlib import Path; from taskwatch.jobs import Jobs; "
        "print(Jobs(Path(sys.argv[1])).submit(json.loads(sys.argv[2]),1).id)"
    )
    args = [sys.executable, "-c", script, str(tmp_path), json.dumps(submitted)]
    children = [subprocess.Popen(args, stdout=subprocess.PIPE, text=True) for _ in range(2)]
    identities = [child.communicate(timeout=30)[0].strip() for child in children]
    assert all(child.returncode == 0 for child in children)
    assert identities[0] == identities[1]
    assert list(Jobs(tmp_path).load()) == [identities[0]]


def test_two_writer_processes_preserve_distinct_updates(tmp_path):
    jobs = Jobs(tmp_path)
    first = jobs.submit(payload(tmp_path), 1)
    second = jobs.submit(payload(tmp_path, "synthetic:second"), 2)
    script = (
        "import json,sys; from pathlib import Path; "
        "from taskwatch.jobs import Jobs,JobState; "
        "Jobs(Path(sys.argv[1])).save(JobState(**json.loads(sys.argv[2])),3)"
    )
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                json.dumps(replace(job, stage="done").to_json()),
            ]
        )
        for job in (first, second)
    ]
    assert [child.wait(timeout=30) for child in children] == [0, 0]
    assert {job.stage for job in Jobs(tmp_path).load().values()} == {"done"}
    events = (jobs.state.dir / "job-changes.jsonl").read_bytes().splitlines()
    assert [json.loads(line)["sequence"] for line in events] == [1, 2, 3, 4]


def test_receipt_immutable_and_globally_unique(tmp_path):
    jobs = Jobs(tmp_path)
    job = jobs.submit(payload(tmp_path), 1)
    with pytest.raises(RecordError, match="receipt"):
        jobs.save(replace(job, receipt_key="changed"), 2)
    with pytest.raises(RecordError, match="receipt"):
        jobs.save(replace(job, id=uuid.uuid4().hex), 2)
    assert jobs.load() == {job.id: job}


def test_conflicting_pending_event_never_replaces_current_state(tmp_path):
    jobs = Jobs(tmp_path)
    job = jobs.submit(payload(tmp_path), 1)
    update = replace(job, stage="done")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _FAULT_WRITER,
            str(tmp_path),
            json.dumps(update.to_json()),
            "prepared",
        ],
        check=False,
    )
    assert child.returncode == 73
    path = jobs.state.dir / "jobs-pending.json"
    pending = json.loads(path.read_bytes())
    event = json.loads(pending["event"])
    event["sequence"] += 1
    event["sha256"] = _digest({k: v for k, v in event.items() if k != "sha256"})
    pending["sequence"] += 1
    pending["event"] = job_store._bytes(event).decode()
    path.write_text(json.dumps(pending))
    with pytest.raises(RecordError, match="sequence"):
        Jobs(tmp_path).load()
    obj = job_store.read_snapshot(jobs.state.dir, job.id, job.receipt_key)
    assert obj["job"]["stage"] == "queued"


def test_failure_before_pending_publication_keeps_old_state(tmp_path, monkeypatch):
    jobs = Jobs(tmp_path)
    job = jobs.submit(payload(tmp_path), 1)
    atomic = job_store._atomic

    def fail(path, data):
        if path.name == "jobs-pending.json":
            raise OSError("synthetic prepare failure")
        atomic(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(job_store, "_atomic", fail)
        with pytest.raises(OSError):
            jobs.save(replace(job, stage="done"), 2)
    assert Jobs(tmp_path).load()[job.id] == job
    assert not (jobs.state.dir / "jobs-pending.json").exists()
    assert list(jobs.folder(job).glob("state-*.tmp"))


def test_prepared_snapshot_digest_mismatch_fails_closed(tmp_path):
    jobs = Jobs(tmp_path)
    job = jobs.submit(payload(tmp_path), 1)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _FAULT_WRITER,
            str(tmp_path),
            json.dumps(replace(job, stage="done").to_json()),
            "prepared",
        ],
        check=False,
    )
    assert child.returncode == 73
    pending = json.loads((jobs.state.dir / "jobs-pending.json").read_bytes())
    staged = jobs.folder(job) / pending["temporary"]
    obj = json.loads(staged.read_bytes())
    obj["job"]["wait_reason"] = "conflicting prepared bytes"
    obj["sha256"] = _digest({"revision": obj["revision"], "job": obj["job"]})
    staged.write_text(json.dumps(obj))
    with pytest.raises(RecordError, match="mismatch"):
        Jobs(tmp_path).load()
    assert (
        job_store.read_snapshot(jobs.state.dir, job.id, job.receipt_key)["job"]["stage"] == "queued"
    )


def test_unindexed_input_copy_folder_is_not_a_job(tmp_path):
    jobs = Jobs(tmp_path)
    original = jobs.submit(payload(tmp_path), 1)
    orphan = jobs.state.dir / "jobs" / ("a" * 32) / "inputs"
    orphan.mkdir(parents=True)
    (orphan / "intent-0.txt").write_text("interrupted synthetic input copy")
    assert Jobs(tmp_path).load() == {original.id: original}

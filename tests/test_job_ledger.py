import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_watch import _Ledger, needs
from taskwatch.jobs import COMPACT_RATIO, Jobs, _digest
from taskwatch.state import RecordError
from tests.test_jobs import payload


def row(job):
    data = job.to_json()
    return json.dumps({"version": 1, "job": data, "sha256": _digest(data)}) + "\n"


def history(tmp_path, count=40):
    jobs = Jobs(tmp_path / "root")
    first = jobs.submit(payload(tmp_path), 1)
    second = jobs.submit(payload(tmp_path, "message:2"), 2)
    path = jobs.state.dir / "jobs.jsonl"
    path.write_text("".join(row(replace(first, attempt=i)) for i in range(count)) + row(second))
    jobs = Jobs(tmp_path / "root")
    jobs.load()
    return jobs, first, second, path


def test_warm_save_and_load_do_not_parse_history(tmp_path, monkeypatch):
    jobs, first, second, path = history(tmp_path)
    parsed = []
    real = json.loads

    def loads(data, *args, **kwargs):
        parsed.append(data)
        return real(data, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    updated = replace(first, attempt=100)
    jobs.save(updated, 10)
    assert len(parsed) == 1
    latest = jobs.load()
    assert latest == {first.id: updated, second.id: second}
    assert len(parsed) == 3  # Only the compacted latest snapshots are read again.
    assert jobs.load() == latest
    assert len(parsed) == 3
    rows = [real(line) for line in path.read_text().splitlines()]
    assert {r["job"]["id"] for r in rows} == {first.id, second.id}
    assert all(r["sha256"] == _digest(r["job"]) for r in rows)


def test_incremental_load_observes_other_writer_and_does_not_expose_cache(tmp_path):
    jobs, first, second, _ = history(tmp_path, 2)
    snapshot = jobs.load()
    snapshot.clear()
    updated = replace(second, stage="user_wait", wait_reason="synthetic")
    Jobs(tmp_path / "root").save(updated, 10)
    assert jobs.load() == {first.id: replace(first, attempt=1), second.id: updated}


@pytest.mark.parametrize("replace_file", [False, True])
def test_load_invalidates_after_shrink_or_replacement(tmp_path, replace_file):
    jobs, first, _, path = history(tmp_path)
    updated = replace(first, attempt=90)
    if replace_file:
        replacement = path.with_suffix(".new")
        replacement.write_text(row(updated) * 60)  # Replacement can be larger, too.
        replacement.replace(path)
    else:
        path.write_text(row(updated))
    assert jobs.load() == {first.id: updated}


def test_conflicting_receipt_cannot_be_saved_or_hidden_by_compaction(tmp_path):
    jobs, first, second, path = history(tmp_path)
    with pytest.raises(RecordError, match="conflicting receipt"):
        jobs.save(replace(second, receipt_key=first.receipt_key), 10)
    with path.open("a") as handle:
        handle.write(row(replace(second, receipt_key=first.receipt_key)))
    for _ in range(2):
        with pytest.raises(RecordError, match="conflicting receipt"):
            jobs.save(first, 11)


def test_torn_tail_is_preserved_without_reading_committed_history(tmp_path, monkeypatch):
    jobs, first, _, path = history(tmp_path, 2)
    fragment = b"x" * 20000
    with path.open("ab") as handle:
        handle.write(fragment)
    real = Path.read_bytes

    def read_bytes(self):
        assert self != path, "save must not read the whole ledger"
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    updated = replace(first, attempt=50)
    jobs.save(updated, 10)
    assert jobs.load()[first.id] == updated
    assert next(path.parent.glob("jobs.jsonl.torn-*")).read_bytes() == fragment


def test_compaction_failure_keeps_durable_snapshots(tmp_path, monkeypatch):
    jobs, first, second, path = history(tmp_path)
    real = os.replace

    def fail(source, dest):
        if dest == path:
            raise OSError("synthetic replace failure")
        return real(source, dest)

    monkeypatch.setattr(os, "replace", fail)
    updated = replace(first, attempt=90)
    with pytest.raises(OSError, match="replace failure"):
        jobs.save(updated, 10)
    assert Jobs(tmp_path / "root").load() == {first.id: updated, second.id: second}


def cursor(root):
    _, text = needs(root)
    return int(next(line[8:] for line in text.splitlines() if line.startswith("cursor: ")))


def test_old_cursor_replays_after_compaction_and_regrowth(tmp_path):
    jobs, first, second, path = history(tmp_path)
    old = cursor(jobs.state.root)
    old_size = path.stat().st_size
    jobs.save(replace(first, stage="done"), 10)
    jobs.save(replace(first, stage="done", error="x" * old_size), 11)
    assert path.stat().st_size > old_size
    code, text = needs(jobs.state.root, cursor=old, wait=True, timeout=1)
    assert code == 0
    assert f"job {first.id}" in text and "1 finished since the cursor" in text
    assert second.id not in text


def test_running_reader_preserves_baseline_across_compaction(tmp_path):
    jobs, first, second, path = history(tmp_path)
    stopped = replace(first, stage="user_wait", wait_reason="synthetic")
    jobs.save(stopped, 5)
    position = cursor(jobs.state.root)
    ledger = _Ledger(path, position)
    ledger.poll()
    baseline = dict(ledger.baseline)
    for i in range(COMPACT_RATIO * 2):
        jobs.save(replace(second, attempt=i), 10 + i)
    ledger.poll()
    assert ledger.baseline == baseline
    assert ledger.rows[first.id]["job"]["stage"] == "user_wait"
    assert ledger.rows[second.id]["job"]["attempt"] == COMPACT_RATIO * 2 - 1
    code, _ = needs(jobs.state.root, cursor=ledger.position(), wait=True, timeout=0)
    assert code == 3


def test_wait_survives_compaction_without_waking_on_unchanged_stop(tmp_path):
    jobs, first, second, _ = history(tmp_path, 2)
    stopped = replace(first, stage="user_wait", wait_reason="synthetic")
    jobs.save(stopped, 3)
    old = cursor(jobs.state.root)
    calls = []

    def sleep(_):
        calls.append(1)
        if len(calls) == 1:
            for i in range(COMPACT_RATIO * 2):
                jobs.save(replace(second, attempt=i), 10 + i)
        else:
            jobs.save(replace(second, stage="user_wait", wait_reason="new"), 50)

    code, text = needs(
        jobs.state.root, cursor=old, wait=True, timeout=100, clock=lambda: 0, sleep=sleep
    )
    assert code == 0 and len(calls) == 2
    assert "2 need you" in text


def test_receipt_identity_cannot_change_before_compaction(tmp_path):
    jobs, first, _, _ = history(tmp_path)
    with pytest.raises(RecordError, match="receipt key changed"):
        jobs.save(replace(first, receipt_key="message:new"), 10)


@pytest.mark.parametrize("prefix", ["jobs-compact-", "jobs-cursors-"])
def test_unsynced_compaction_is_not_published(tmp_path, monkeypatch, prefix):
    jobs, first, second, path = history(tmp_path)
    original_inode = path.stat().st_ino
    real = os.fsync

    def fail(fd):
        if Path(os.readlink(f"/proc/self/fd/{fd}")).name.startswith(prefix):
            raise OSError("synthetic sync failure")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fail)
    updated = replace(first, attempt=90)
    with pytest.raises(OSError, match="sync failure"):
        jobs.save(updated, 10)
    assert path.stat().st_ino == original_inode
    assert Jobs(tmp_path / "root").load() == {first.id: updated, second.id: second}
    monkeypatch.undo()
    jobs.save(updated, 11)
    assert Jobs(tmp_path / "root").load() == {first.id: updated, second.id: second}

"""Read-only v2 watch behavior on isolated synthetic roots."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_store, job_watch
from taskwatch.job_cursor import decode, encode
from taskwatch.jobs import Jobs, _digest
from taskwatch.state import RecordError, state_dir
from tests.test_jobs import payload
from tests.test_needs import BRIEF, CLEAN_BRIEF, CLEAN_PASS, Ledger, accepted_done


def _cursor(text):
    return int(next(line[8:] for line in text.splitlines() if line.startswith("cursor: ")))


def _waiting(tmp_path):
    root = tmp_path / "root"
    jobs = Jobs(root)
    job = jobs.submit(payload(tmp_path), 1)
    job = replace(job, stage="user_wait", wait_reason="synthetic_wait")
    jobs.save(job, 2)
    return root, jobs, job


def _files(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_empty_watch_never_creates_root(tmp_path):
    root = tmp_path / "absent"
    code, text = job_watch.needs(root)
    assert code == 0 and _cursor(text) == 0
    assert not root.exists()


@pytest.mark.parametrize("artifact", ["jobs.jsonl", "job-changes.jsonl", "jobs-pending.json"])
def test_first_watch_requests_authoritative_initialization(tmp_path, artifact):
    root = tmp_path / "root"
    directory = state_dir(root)
    directory.mkdir(parents=True)
    (directory / artifact).write_text("")
    before = _files(root)
    with pytest.raises(RecordError, match="show.*scan"):
        job_watch.needs(root)
    assert _files(root) == before


def test_watch_has_only_projections_and_does_not_lock_or_write(tmp_path, monkeypatch):
    root, _, job = _waiting(tmp_path)
    before = _files(root)

    def forbidden(*args, **kwargs):
        raise AssertionError("watch acquired writer lock")

    monkeypatch.setattr("taskwatch.state.State.lock", forbidden)
    code, text = job_watch.needs(root)
    assert code == 0 and job.id in text
    assert _files(root) == before
    ledger = job_watch._Ledger(state_dir(root) / "job-changes.jsonl", _cursor(text))
    assert ledger.poll()
    assert all("job" not in row and "projection" in row for row in ledger.rows.values())
    assert "Preserve this full original prompt" not in json.dumps(ledger.rows)


@pytest.mark.parametrize("kind", ["byte", "old_generation", "other_v2", "future", "split"])
def test_unusable_cursor_replays_current_wait_and_done(tmp_path, kind):
    root, jobs, job = _waiting(tmp_path)
    done = jobs.submit(payload(tmp_path, receipt="second"), 3)
    jobs.save(replace(done, stage="done"), 4)
    position = _cursor(job_watch.needs(root)[1])
    generation, offset = decode(position)
    positions = {
        "byte": 1,
        "old_generation": (123 << 64) | 1,
        "other_v2": encode("f" * 32 if generation != "f" * 32 else "e" * 32, 0),
        "future": encode(generation, offset + 100),
        "split": encode(generation, 1),
    }
    code, text = job_watch.needs(root, cursor=positions[kind])
    assert code == 0 and "replaying current items" in text
    assert job.id in text and done.id in text
    assert _cursor(text) == position
    _, precise = job_watch.needs(root, cursor=position)
    assert "replaying" not in precise and done.id not in precise


def test_current_cursor_ignores_same_stop_and_observes_new_entry(tmp_path):
    root, jobs, job = _waiting(tmp_path)
    cursor = _cursor(job_watch.needs(root)[1])
    now = [0]

    def sleep(seconds):
        now[0] += seconds
        if now[0] == 1:
            jobs.save(job, 3)
        else:
            jobs.save(replace(job, events=(*job.events, {"type": "wait", "at": 4})), 4)

    code, text = job_watch.needs(
        root,
        cursor=cursor,
        wait=True,
        timeout=3,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == 0 and now[0] == 2 and job.id in text


def test_selected_snapshot_missing_or_corrupt_is_not_hidden(tmp_path):
    root, _, job = _waiting(tmp_path)
    path = state_dir(root) / "jobs" / job.id / "state.json"
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(RecordError):
        job_watch.needs(root)
    path.write_bytes(original.replace(b"synthetic_wait", b"synthetic_fail"))
    with pytest.raises(RecordError):
        job_watch.needs(root)


@pytest.mark.parametrize("damage", ["checksum", "sequence"])
def test_every_complete_event_is_validated_even_for_unselected_jobs(tmp_path, damage):
    root, jobs, job = _waiting(tmp_path)
    jobs.save(replace(job, stage="work"), 3)
    path = state_dir(root) / "job-changes.jsonl"
    lines = path.read_text().splitlines()
    event = json.loads(lines[0])
    if damage == "checksum":
        event["sha256"] = "0" * 64
    else:
        event["sequence"] += 1
        event["sha256"] = _digest({k: v for k, v in event.items() if k != "sha256"})
    path.write_text("\n".join([json.dumps(event), *lines[1:]]) + "\n")
    with pytest.raises(RecordError):
        job_watch.needs(root)


def test_torn_tail_never_becomes_a_cursor_boundary(tmp_path):
    root, _, _ = _waiting(tmp_path)
    before = _cursor(job_watch.needs(root)[1])
    path = state_dir(root) / "job-changes.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"version":2')
    assert _cursor(job_watch.needs(root)[1]) == before
    assert path.read_bytes().endswith(b'{"version":2')


def test_prepared_newer_snapshot_defers_without_advancing_then_normal_load_recovers(
    tmp_path,
    monkeypatch,
):
    root, jobs, job = _waiting(tmp_path)
    cursor = _cursor(job_watch.needs(root)[1])
    original_sync = job_store._fsync

    def crash(path):
        if Path(path).name == "state.json":
            raise OSError("synthetic power loss after rename")
        return original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(job_store, "_fsync", crash)
        with pytest.raises(OSError, match="synthetic power loss"):
            jobs.save(replace(job, stage="done"), 3)
    before = _files(root)
    code, text = job_watch.needs(root, cursor=cursor)
    assert code == 0 and "publication pending" in text and _cursor(text) == cursor
    assert job.id not in text and _files(root) == before
    now = [0]

    def recover(seconds):
        now[0] += seconds
        assert Jobs(root).load()[job.id].stage == "done"

    code, text = job_watch.needs(
        root,
        cursor=cursor,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=recover,
    )
    assert code == 0 and now[0] == 1 and job.id in text
    assert "FINISHED SINCE THE CURSOR" in text and _cursor(text) > cursor


def test_event_arriving_during_snapshot_read_retries_to_current_state(tmp_path, monkeypatch):
    root, jobs, job = _waiting(tmp_path)
    original = job_watch.read_snapshot
    reads = []

    def changing_read(directory, ident, receipt):
        row = original(directory, ident, receipt)
        reads.append(ident)
        if len(reads) == 1:
            jobs.save(replace(job, wait_reason="new_current_reason"), 3)
        return row

    monkeypatch.setattr(job_watch, "read_snapshot", changing_read)
    code, text = job_watch.needs(root)
    assert code == 0 and "new_current_reason" in text
    assert "synthetic_wait" not in text and len(reads) >= 3


def test_cursor_discriminator_rejects_reserved_bits_and_legacy_values():
    generation = "0123456789abcdef" * 2
    value = encode(generation, 17)
    assert decode(value) == (generation, 17)
    assert decode(value | (1 << 200)) is None
    assert decode(17) is None and decode(-1) is None


def test_empty_wait_observes_first_published_generation(tmp_path):
    root = tmp_path / "root"
    now = [0]

    def publish(seconds):
        now[0] += seconds
        assert not root.exists()
        jobs = Jobs(root)
        job = jobs.submit(payload(tmp_path), 1)
        jobs.save(replace(job, stage="user_wait", wait_reason="first_stop"), 2)

    code, text = job_watch.needs(
        root,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=publish,
    )
    assert code == 0 and now[0] == 1 and "first_stop" in text
    assert decode(_cursor(text)) is not None


def test_gate_loads_and_validates_current_predecessor_state(tmp_path):
    root, jobs, pred = _waiting(tmp_path)
    jobs.save(replace(pred, stage="done"), 3)
    gate = jobs.submit(payload(tmp_path, receipt="gate") | {"predecessors": [pred.id]}, 4)
    jobs.save(replace(gate, stage="approval_wait"), 5)
    _, text = job_watch.needs(root)
    assert pred.id in text and gate.id in text
    state = state_dir(root) / "jobs" / pred.id / "state.json"
    state.write_text("not json")
    with pytest.raises(RecordError):
        job_watch.needs(root)


def test_snapshot_replaced_during_render_is_not_reported_as_current(tmp_path, monkeypatch):
    root, jobs, job = _waiting(tmp_path)
    cursor = _cursor(job_watch.needs(root)[1])
    original_lines = job_watch._lines
    original_sync = job_store._fsync

    def crash(path):
        if Path(path).name == "state.json":
            raise OSError("synthetic crash")
        return original_sync(path)

    def racing_lines(root, rows, item):
        rendered = original_lines(root, rows, item)
        with monkeypatch.context() as patch:
            patch.setattr(job_store, "_fsync", crash)
            with pytest.raises(OSError, match="synthetic crash"):
                jobs.save(replace(job, stage="done"), 3)
        return rendered

    monkeypatch.setattr(job_watch, "_lines", racing_lines)
    code, text = job_watch.needs(root, cursor=cursor)
    assert code == 0 and "publication pending" in text and _cursor(text) == cursor
    assert job.id not in text


def test_newer_snapshot_without_prepared_transaction_is_corruption(tmp_path):
    root, jobs, job = _waiting(tmp_path)
    stream = jobs.state.dir / "job-changes.jsonl"
    committed = stream.read_bytes()
    jobs.save(replace(job, wait_reason="later"), 3)
    stream.write_bytes(committed)
    with pytest.raises(RecordError):
        job_watch.needs(root)


def test_wait_timeout_survives_continuously_changing_boundary(tmp_path, monkeypatch):
    root, jobs, job = _waiting(tmp_path)
    cursor = _cursor(job_watch.needs(root)[1])
    original = job_watch._Ledger.poll
    now = [0]

    def racing_poll(ledger):
        original(ledger)
        now[0] += 1
        jobs.save(replace(job, attempt=now[0]), now[0] + 3)
        return False

    monkeypatch.setattr(job_watch._Ledger, "poll", racing_poll)
    code, _ = job_watch.needs(
        root,
        cursor=cursor,
        wait=True,
        timeout=3,
        clock=lambda: now[0],
    )
    assert code == job_watch.TIMED_OUT and now[0] == 3


@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.parametrize("damage", ["receipt", "sequence"])
def test_inconsistent_pending_cannot_hide_stable_corruption(
    tmp_path, monkeypatch, creation, damage
):
    root, jobs, job = _waiting(tmp_path)
    atomic = job_store._atomic
    sync = job_store._fsync

    def after_index(path, data):
        atomic(path, data)
        if path.name == "jobs-store.json":
            raise OSError("synthetic index publication crash")

    def after_snapshot(path):
        if path.name == "state.json":
            raise OSError("synthetic snapshot publication crash")
        return sync(path)

    update = replace(job, id="b" * 32, receipt_key="new") if creation else job
    with monkeypatch.context() as patch:
        patch.setattr(job_store, "_atomic", after_index if creation else atomic)
        patch.setattr(job_store, "_fsync", sync if creation else after_snapshot)
        with pytest.raises(OSError):
            jobs.save(update, 3)
    path = jobs.state.dir / "jobs-pending.json"
    pending = json.loads(path.read_bytes())
    if damage == "receipt":
        pending["receipt"] = "conflicting"
    else:
        event = json.loads(pending["event"])
        pending["sequence"] += 1
        event["sequence"] = pending["sequence"]
        event["sha256"] = _digest({k: v for k, v in event.items() if k != "sha256"})
        pending["event"] = job_store._bytes(event).decode()
    path.write_text(json.dumps(pending))
    with pytest.raises(RecordError):
        job_watch.needs(root)


def test_quiet_completion_classification_is_cached_until_its_revision_changes(
    tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path)
    ledger.add(stage="work")
    cursor = ledger.size()
    ident = accepted_done(ledger)
    original = job_watch.brief.problems
    classifications = []
    observed = []

    def classify(path, *args, **kwargs):
        classifications.append(path)
        return original(path, *args, **kwargs)

    def sleep(seconds):
        observed.append(len(classifications))
        if len(observed) == 2:
            ledger.set(ident, error="Updated completion metadata")
        elif len(observed) == 3:
            folder = ledger.jobs.folder(ledger.get(ident))
            (folder / "brief.md").write_text(BRIEF, encoding="utf-8")
            ledger.set(ident, error="Decision added")
        elif len(observed) > 3:
            pytest.fail("revised completion did not wake the watcher")

    monkeypatch.setattr(job_watch.brief, "problems", classify)
    code, text = ledger.watch(cursor, sleep)
    assert code == 0 and ident in text and "decisions: 2" in text
    assert observed == [1, 1, 2]
    assert len(classifications) == 3


def test_snapshot_ahead_of_clean_candidate_defers_classification_until_recovery(
    tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path)
    ident = ledger.add(stage="work")
    cursor = ledger.size()
    folder = ledger.jobs.folder(ledger.get(ident))
    (folder / "a.py").write_text("value = 1\n", encoding="utf-8")
    (folder / "brief.md").write_text(CLEAN_BRIEF, encoding="utf-8")
    ledger.set(ident, stage="done", reviews=(CLEAN_PASS,))
    sync = job_store._fsync

    def crash(path):
        if Path(path).name == "state.json":
            raise OSError("synthetic pending completion")
        return sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(job_store, "_fsync", crash)
        with pytest.raises(OSError, match="synthetic pending completion"):
            ledger.set(ident, reviews=({"verdict": "FAIL", "findings": []},))
    before = _files(ledger.root)
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    code, text = job_watch.needs(
        ledger.root,
        cursor=cursor,
        wait=True,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == job_watch.TIMED_OUT and "cursor:" not in text
    assert _files(ledger.root) == before
    assert ledger.jobs.load()[ident].reviews[-1]["verdict"] == "FAIL"
    code, text = job_watch.needs(ledger.root, cursor=cursor, wait=True)
    assert code == 0 and ident in text and "done FAIL" in text


@pytest.mark.parametrize("kind", ["byte", "old_generation", "future"])
def test_quiet_replay_classifies_clean_completion_before_waiting_for_gate(tmp_path, kind):
    ledger = Ledger(tmp_path)
    done = accepted_done(ledger)
    position = ledger.size()
    generation, offset = decode(position)
    cursor = {
        "byte": 1,
        "old_generation": encode("f" * 32 if generation != "f" * 32 else "e" * 32, 0),
        "future": encode(generation, offset + 100),
    }[kind]
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        ledger.add(stage="approval_wait", wait_reason="approval_required")

    code, text = ledger.watch(cursor, sleep)
    assert code == 0 and len(calls) == 1
    assert "replaying current items" in text and "1 gate" in text and done in text
    assert decode(_cursor(text))[0] == generation


@pytest.mark.parametrize("include_done", [False, True])
def test_quiet_completion_watch_is_read_only(tmp_path, monkeypatch, include_done):
    ledger = Ledger(tmp_path)
    cursor = ledger.size()
    accepted_done(ledger)
    before = _files(ledger.root)
    now = [0]

    def forbidden(*args, **kwargs):
        raise AssertionError("watch acquired writer lock")

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr("taskwatch.state.State.lock", forbidden)
    code, _ = job_watch.needs(
        ledger.root,
        cursor=cursor,
        wait=True,
        include_done=include_done,
        timeout=2,
        interval=1,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert code == (0 if include_done else job_watch.TIMED_OUT)
    assert _files(ledger.root) == before


def test_clean_candidate_snapshot_corruption_is_not_attention_fallback(tmp_path):
    ledger = Ledger(tmp_path)
    cursor = ledger.size()
    ident = accepted_done(ledger)
    path = ledger.jobs.folder(ledger.get(ident)) / "state.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["job"]["error"] = "synthetic checksum mismatch"
    path.write_text(json.dumps(row), encoding="utf-8")
    before = _files(ledger.root)
    with pytest.raises(RecordError):
        job_watch.needs(ledger.root, cursor=cursor, wait=True)
    assert _files(ledger.root) == before


@pytest.mark.parametrize("damage", ["missing", "checksum"])
def test_cached_quiet_completion_does_not_hide_later_storage_corruption(tmp_path, damage):
    ledger = Ledger(tmp_path)
    cursor = ledger.size()
    ident = accepted_done(ledger)
    path = ledger.jobs.folder(ledger.get(ident)) / "state.json"
    now = [0]

    def sleep(seconds):
        now[0] += seconds
        if now[0] == 1:
            if damage == "missing":
                path.unlink()
            else:
                row = json.loads(path.read_text(encoding="utf-8"))
                row["job"]["error"] = "synthetic post-classification corruption"
                path.write_text(json.dumps(row), encoding="utf-8")

    with pytest.raises(RecordError):
        job_watch.needs(
            ledger.root,
            cursor=cursor,
            wait=True,
            timeout=2,
            interval=1,
            clock=lambda: now[0],
            sleep=sleep,
        )

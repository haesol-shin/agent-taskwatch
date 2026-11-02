"""Legacy ledger refusal and immutable backup at the v2 cutover."""

import json
from dataclasses import replace

import pytest

from taskwatch.job_watch import needs
from taskwatch.jobs import Jobs, _digest
from taskwatch.state import RecordError
from tests.test_jobs import payload


def row(job):
    data = job.to_json()
    return json.dumps({"version": 1, "job": data, "sha256": _digest(data)}) + "\n"


def legacy(tmp_path):
    donor = Jobs(tmp_path / "donor")
    first = donor.submit(payload(tmp_path), 1)
    second = donor.submit(payload(tmp_path, "message:2"), 2)
    jobs = Jobs(tmp_path / "root")
    jobs.state.dir.mkdir(parents=True)
    path = jobs.state.dir / "jobs.jsonl"
    return jobs, first, second, path


def test_cutover_preserves_latest_fields_and_never_dual_writes(tmp_path):
    jobs, first, second, path = legacy(tmp_path)
    latest = replace(first, stage="user_wait", wait_reason="synthetic", error="full history")
    content = (row(first) + row(second) + row(latest)).encode()
    path.write_bytes(content)
    cursors = path.with_suffix(".cursors.json")
    cursors.write_text("[]")
    assert jobs.load() == {first.id: latest, second.id: second}
    jobs.save(replace(second, stage="done"), 3)
    assert Jobs(jobs.state.root).load()[second.id].stage == "done"
    assert path.read_bytes() == content
    _, text = needs(jobs.state.root, cursor=0)
    assert first.id in text and second.id in text
    assert cursors.read_text() == "[]"


@pytest.mark.parametrize("mutation", ["receipt", "identity", "version", "checksum", "schema"])
def test_migration_validates_every_committed_revision(tmp_path, mutation):
    jobs, first, second, path = legacy(tmp_path)
    bad = json.loads(row(first))
    if mutation == "receipt":
        bad = json.loads(row(replace(second, receipt_key=first.receipt_key)))
    elif mutation == "identity":
        bad = json.loads(row(replace(first, receipt_key="message:changed")))
    elif mutation == "version":
        bad["version"] = 99
    elif mutation == "checksum":
        bad["job"]["stage"] = "done"
    else:
        bad["job"]["attempt"] = True
        bad["sha256"] = _digest(bad["job"])
    path.write_text(row(first) + json.dumps(bad) + "\n" + row(first))
    with pytest.raises(RecordError):
        jobs.load()
    assert not (jobs.state.dir / "jobs-store.json").exists()


def test_legacy_torn_suffix_is_preserved_before_cutover(tmp_path):
    jobs, first, _, path = legacy(tmp_path)
    fragment = b'{"version":1,"job":'
    committed = row(first).encode()
    path.write_bytes(committed + fragment)
    assert jobs.load()[first.id] == first
    assert path.read_bytes() == committed
    assert next(path.parent.glob("jobs.jsonl.torn-*")).read_bytes() == fragment
    jobs.save(replace(first, stage="done"), 4)
    assert path.read_bytes() == committed


def test_first_needs_refuses_legacy_without_writing_or_locking(tmp_path):
    jobs, first, _, path = legacy(tmp_path)
    path.write_text(row(first))
    before = {p: p.read_bytes() for p in jobs.state.dir.rglob("*") if p.is_file()}
    with pytest.raises(RecordError, match="show|scan"):
        needs(jobs.state.root)
    after = {p: p.read_bytes() for p in jobs.state.dir.rglob("*") if p.is_file()}
    assert after == before

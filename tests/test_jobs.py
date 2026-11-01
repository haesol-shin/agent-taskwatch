import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main
from taskwatch.jobs import Jobs, load_job_config
from taskwatch.state import RecordError


def payload(tmp_path: Path, receipt="message:1") -> dict:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    intent = source / "intent.md"
    spec = source / "spec.md"
    intent.write_text("Approved synthetic intent", encoding="utf-8")
    spec.write_text("Synthetic deliverable specification", encoding="utf-8")
    return {
        "receipt_key": receipt,
        "approval_source": "message-1",
        "approval_scope": "local files",
        "prompt": "Preserve this full original prompt",
        "review": "source",
        "target": str(source),
        "intent": str(intent),
        "spec": str(spec),
        "outputs": ["result.txt"],
        "check_command": ["true"],
        "checklist": "checklist.json",
        "progress": "progress.md",
    }


def test_idempotent_submit_copies_inputs_and_preserves_original(tmp_path):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    first = jobs.submit(data, 10)
    Path(data["intent"]).write_text("Changed after receipt", encoding="utf-8")
    repeated = jobs.submit({"receipt_key": data["receipt_key"]}, 20)
    assert repeated == first
    assert first.prompt == data["prompt"]
    assert Path(first.intent).read_text() == "Approved synthetic intent"
    for name, digest in first.input_hashes.items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
    assert len(jobs.state.read_records("jobs.jsonl")) == 1


def test_concurrent_receipt_has_one_durable_identity(tmp_path):
    data = payload(tmp_path)
    root = tmp_path / "root"
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(lambda _: Jobs(root).submit(data).id, range(8)))
    assert len(set(identifiers)) == 1
    assert len(Jobs(root).load()) == 1


def test_corrupt_committed_record_blocks_reads_and_new_submit(tmp_path):
    jobs = Jobs(tmp_path / "root")
    jobs.submit(payload(tmp_path), 10)
    path = jobs.state.dir / "jobs.jsonl"
    data = json.loads(path.read_text())
    data["job"]["stage"] = "deliver_pending"
    path.write_text(json.dumps(data) + "\n")
    with pytest.raises(RecordError, match="corrupt"):
        jobs.load()
    with pytest.raises(RecordError):
        jobs.submit(payload(tmp_path, "message:2"), 20)


def test_torn_tail_preserves_receipt_and_is_repaired_before_append(tmp_path):
    jobs = Jobs(tmp_path / "root")
    first = jobs.submit(payload(tmp_path), 10)
    path = jobs.state.dir / "jobs.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"version":1,"job":')
    assert jobs.load()[first.id] == first
    jobs.submit(payload(tmp_path, "message:2"), 20)
    assert len(jobs.load()) == 2
    assert len(list(path.parent.glob("jobs.jsonl.torn-*"))) == 1


def test_changed_copied_input_is_not_accepted(tmp_path):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    Path(job.spec).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        jobs.verify_inputs(job)


@pytest.mark.parametrize(
    "field,value",
    [
        ("outputs", ["../escape"]),
        ("checklist", "/placeholder/checklist.json"),
        ("check_command", []),
        ("approval_required", "yes"),
        ("expected_wait_seconds", -1),
    ],
)
def test_submit_refuses_unsafe_or_incomplete_contract(tmp_path, field, value):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    data[field] = value
    with pytest.raises(ValueError):
        jobs.submit(data)
    assert jobs.load() == {}


def test_standalone_init_and_submit_without_hermes(tmp_path, capsys):
    root = tmp_path / "root"
    assert main(["--root", str(root), "init"]) == 0
    assert load_job_config(root).slots == 2
    file = tmp_path / "job.json"
    file.write_text(json.dumps(payload(tmp_path)), encoding="utf-8")
    assert main(["--root", str(root), "submit", str(file)]) == 0
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert Jobs(root).load()[receipt["id"]].stage == "queued"


@pytest.mark.parametrize(
    "change",
    [
        {"approval_required": 0},
        {"input_hashes": {}},
        {"outputs": []},
        {"check_command": [None]},
    ],
)
def test_valid_checksum_does_not_allow_invalid_job_schema(tmp_path, change):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    data = {**job.to_json(), **change}
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row = {"version": 1, "job": data, "sha256": hashlib.sha256(encoded).hexdigest()}
    path = jobs.state.dir / "jobs.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    with pytest.raises(RecordError):
        jobs.load()


def test_repeated_approval_wait_preserves_queued_resume_step(tmp_path, capsys):
    root = tmp_path / "root"
    jobs = Jobs(root)
    job = jobs.submit(payload(tmp_path), 10)
    job = replace(job, stage="queued", resume_stage="review", attempt=3)
    jobs.save(job, 11)
    for action in ("wait", "approve", "wait", "approve"):
        assert main(["--root", str(root), action, job.id, "--reason", "message-2"]) == 0
    current = jobs.load()[job.id]
    assert (current.stage, current.resume_stage, current.attempt) == (
        "queued",
        "review",
        3,
    )


def test_approval_records_exact_replacement_scope(tmp_path, capsys):
    root = tmp_path / "root"
    jobs = Jobs(root)
    job = jobs.submit(payload(tmp_path), 10)
    assert main(["--root", str(root), "wait", job.id, "--reason", "Need target write"]) == 0
    assert (
        main(
            [
                "--root",
                str(root),
                "approve",
                job.id,
                "--reason",
                "message-2",
                "--scope",
                "Write only the approved target",
            ]
        )
        == 0
    )
    current = jobs.load()[job.id]
    assert current.approval_source.startswith("message-1\nGate ")
    assert current.approval_source.endswith(": message-2")
    assert current.approval_scope == "Write only the approved target"
    assert current.stage == "queued"


def _stage_job(tmp_path, stage, **changes):
    root = tmp_path / "root"
    jobs = Jobs(root)
    job = jobs.submit(payload(tmp_path))
    if stage != "queued":
        jobs.save(replace(job, stage=stage, **changes), 1.0)
    return root, jobs, job


@pytest.mark.parametrize(
    "stage", ["approval_wait", "user_wait", "deliver_pending", "done", "cancelled"]
)
def test_watch_exits_at_once_when_already_stopped(tmp_path, capsys, stage):
    root, _, job = _stage_job(tmp_path, stage, wait_reason="needs a decision", error="boom")
    assert main(["--root", str(root), "watch", job.id]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"job {job.id}: {stage}")
    assert len(out.splitlines()) <= 20
    assert ("reason: needs a decision" in out) == True  # noqa: E712
    assert "error: recorded" in out and "boom" not in out


def test_watch_wakes_on_stage_change_and_ignores_other_snapshots(tmp_path):
    from taskwatch.job_watch import watch

    root, jobs, job = _stage_job(tmp_path, "queued")
    other = jobs.submit(payload(tmp_path, receipt="message:2"))
    steps = iter(["work", "other", "stop"])

    def sleep(_):
        step = next(steps)
        if step == "work":
            jobs.save(replace(job, stage="work", notices=({"status": "success"},)), 2.0)
        elif step == "other":
            jobs.save(replace(other, stage="done"), 3.0)
        else:
            jobs.save(replace(job, stage="user_wait", wait_reason="stuck"), 4.0)

    code, report = watch(root, job.id, timeout=100, interval=5, sleep=sleep, clock=lambda: 0)
    assert code == 0
    assert "user_wait" in report and "reason: stuck" in report


def test_watch_times_out_with_exit_3(tmp_path, capsys):
    from taskwatch.job_watch import watch

    root, _, job = _stage_job(tmp_path, "queued")
    now = [0.0]
    code, report = watch(
        root,
        job.id,
        timeout=12,
        interval=5,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert code == 3 and "time limit" in report and now[0] == 12


def test_watch_unknown_job_exits_2_and_usage_errors_exit_2(tmp_path, capsys):
    root, _, _ = _stage_job(tmp_path, "queued")
    assert main(["--root", str(root), "watch", "nope"]) == 2
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(root), "watch", "nope", "--interval", "4"])
    assert exc.value.code == 2


def test_watch_never_writes_the_state_folder(tmp_path):
    root, _, job = _stage_job(tmp_path, "done")

    def snapshot():
        return {
            p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (root / "taskwatch").rglob("*")
        }

    before = snapshot()
    assert main(["--root", str(root), "watch", job.id]) == 0
    assert main(["--root", str(root), "watch", "nope"]) == 2
    assert snapshot() == before
    empty = tmp_path / "empty"
    assert main(["--root", str(empty), "watch", "x"]) == 2
    assert not empty.exists()


def test_watch_report_hides_error_text_keeps_output_paths_and_line_limit(tmp_path, capsys):
    root, _, job = _stage_job(
        tmp_path,
        "user_wait",
        error="Authorization: Bearer SYNTHETIC_SECRET",
        workspace="/ws\n" * 30,
        outputs=("a\n" * 30,),
    )
    assert main(["--root", str(root), "watch", job.id]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC_SECRET" not in out
    assert "output:" in out
    assert len(out.splitlines()) <= 20


def test_watch_refuses_unknown_record_version(tmp_path):
    root, _, job = _stage_job(tmp_path, "done")
    ledger = root / "taskwatch" / "state" / "jobs.jsonl"
    row = json.loads(ledger.read_text().splitlines()[-1])
    row["version"] = 2
    ledger.write_text(json.dumps(row) + "\n")
    assert main(["--root", str(root), "watch", job.id]) == 1


@pytest.mark.parametrize(
    "old,new",
    [("code", "code"), ("design", "design"), ("document", "source"), ("research", "source")],
)
def test_record_written_with_the_old_kind_field_still_loads(tmp_path, old, new):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    data = {key: value for key, value in job.to_json().items() if key != "review"}
    data["kind"] = old
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row = {"version": 1, "job": data, "sha256": hashlib.sha256(encoded).hexdigest()}
    path = jobs.state.dir / "jobs.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert jobs.load()[job.id].review == new


def test_watch_reads_an_old_record_with_kind(tmp_path, capsys):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    data = {key: value for key, value in job.to_json().items() if key != "review"}
    data["kind"], data["stage"] = "research", "done"
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row = {"version": 1, "job": data, "sha256": hashlib.sha256(encoded).hexdigest()}
    (jobs.state.dir / "jobs.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert main(["--root", str(tmp_path / "root"), "watch", job.id]) == 0
    assert capsys.readouterr().out.startswith(f"job {job.id}: done")


def test_new_submission_with_kind_is_refused_and_names_review(tmp_path):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    data["kind"] = data.pop("review")
    with pytest.raises(ValueError, match="`review`"):
        jobs.submit(data)
    assert jobs.load() == {}


def test_submission_refuses_the_retired_review_values(tmp_path):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    data["review"] = "research"
    with pytest.raises(ValueError, match="code, design, or source"):
        jobs.submit(data)

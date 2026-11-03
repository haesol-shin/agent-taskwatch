import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main
from taskwatch.jobs import Jobs, load_job_config
from taskwatch.state import RecordError
from tests.spec_fixture import VALID_SPEC


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


@pytest.mark.parametrize(
    "outputs,error",
    [
        (["design.md"], "choices.json"),
        (["nested/choices.json"], "choices.json"),
        (["../choices.json"], "stay in the workspace"),
        ([42], "outputs needs nonempty strings"),
        ("choices.json", "outputs must be"),
    ],
)
def test_design_rejects_incomplete_outputs_before_copy(tmp_path, outputs, error):
    jobs = Jobs(tmp_path / "root")
    data = _code_payload(tmp_path, "design", review="design", outputs=outputs)
    with pytest.raises(ValueError, match=error):
        jobs.submit(data)
    assert not jobs.load()
    assert not (jobs.state.dir / "jobs").exists()
    assert data["outputs"] == outputs


@pytest.mark.parametrize("output", ["choices.json", "./choices.json"])
def test_design_root_output_and_receipt_replay(tmp_path, output):
    jobs = Jobs(tmp_path / "root")
    data = _code_payload(tmp_path, "design", review="design", outputs=["design.md", output])
    first = jobs.submit(data)
    assert first.outputs == ("design.md", output)
    Path(data["intent"]).unlink()
    repeated = jobs.submit({**data, "outputs": ["design.md"]})
    assert repeated == first
    assert Path(first.intent).is_file()
    assert list(jobs.load()) == [first.id]


def test_design_with_dot_slash_choices_still_passes_the_end_check(tmp_path):
    from taskwatch.job_runner import _brief_problems

    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(
        _code_payload(tmp_path, "design", review="design", outputs=["./choices.json"]), 10
    )
    problems = _brief_problems(jobs, job, tmp_path, tmp_path)
    assert not any("must be listed in outputs" in problem for problem in problems)


def test_review_only_design_does_not_require_worker_outputs(tmp_path):
    jobs = Jobs(tmp_path / "root")
    data = payload(tmp_path)
    for key in ("outputs", "check_command", "checklist", "progress"):
        del data[key]
    data.update(review="design", review_target={"files": ["intent.md"]})
    job = jobs.submit(data)
    assert job.outputs == ("intent.md",)
    assert job.review_target == {"files": ("intent.md",)}


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
    index = json.loads((jobs.state.dir / "jobs-store.json").read_text())
    assert index["receipts"] == {first.id: first.receipt_key}


def test_concurrent_receipt_has_one_durable_identity(tmp_path):
    data = payload(tmp_path)
    root = tmp_path / "root"
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(lambda _: Jobs(root).submit(data).id, range(8)))
    assert len(set(identifiers)) == 1
    assert len(Jobs(root).load()) == 1


def test_corrupt_committed_record_blocks_reads_and_new_submit(tmp_path):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    path = jobs.folder(job) / "state.json"
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
    path = jobs.state.dir / "job-changes.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"version":2,"generation":')
    assert jobs.load()[first.id] == first
    jobs.submit(payload(tmp_path, "message:2"), 20)
    assert len(jobs.load()) == 2
    assert len(list(path.parent.glob("job-changes.jsonl.torn-*"))) == 1


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
    content = {"revision": 1, "job": data}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row = {"version": 2, **content, "sha256": hashlib.sha256(encoded).hexdigest()}
    path = jobs.folder(job) / "state.json"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
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


@pytest.mark.parametrize(
    "old,new",
    [("code", "code"), ("design", "design"), ("document", "source"), ("research", "source")],
)
def test_record_written_with_the_old_kind_field_still_loads(tmp_path, old, new):
    jobs = Jobs(tmp_path / "root")
    job = Jobs(tmp_path / "donor").submit(payload(tmp_path), 10)
    data = {key: value for key, value in job.to_json().items() if key != "review"}
    data["kind"] = old
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    row = {"version": 1, "job": data, "sha256": hashlib.sha256(encoded).hexdigest()}
    path = jobs.state.dir / "jobs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert jobs.load()[job.id].review == new


def test_watch_subcommand_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(tmp_path / "root"), "watch", "x"])
    assert exc.value.code == 2


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


def _code_payload(tmp_path: Path, receipt: str, **extra: object) -> dict:
    data = payload(tmp_path, receipt)
    Path(data["spec"]).write_text(VALID_SPEC, encoding="utf-8")
    return {**data, "review": "code", **extra}


def test_submit_resolves_receipt_keys_in_predecessors_and_base(tmp_path):
    root = tmp_path / "root"
    jobs = Jobs(root)
    first = jobs.submit(_code_payload(tmp_path, "msg-1:1"), 10)
    second = jobs.submit(
        _code_payload(
            tmp_path,
            "msg-1:2",
            predecessors=["msg-1:1"],
            base="@msg-1:1",
        ),
        20,
    )
    third = jobs.submit(
        _code_payload(
            tmp_path,
            "msg-1:3",
            predecessors=[first.id, "msg-1:2"],
            base="@msg-1:2",
        ),
        30,
    )
    assert second.predecessors == (first.id,)
    assert second.base == f"taskwatch/{first.id[:8]}"
    assert third.predecessors == (first.id, second.id)
    assert third.base == f"taskwatch/{second.id[:8]}"
    reloaded = jobs.load()
    assert reloaded[second.id].predecessors == (first.id,)
    assert reloaded[second.id].base == f"taskwatch/{first.id[:8]}"


def test_submit_receipt_base_uses_configured_branch_prefix_and_rejects_unknown_tokens(tmp_path):
    root = tmp_path / "root"
    assert main(["--root", str(root), "init"]) == 0
    cfg_path = root / "taskwatch" / "jobs-config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["branch_prefix"] = "work/"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    jobs = Jobs(root)
    source_job = jobs.submit(payload(tmp_path, "msg-1:src"), 10)
    code_job = jobs.submit(_code_payload(tmp_path, "msg-1:code"), 20)
    stacked = jobs.submit(
        _code_payload(
            tmp_path,
            "msg-1:next",
            predecessors=["msg-1:src", "msg-1:code"],
            base="@msg-1:code",
        ),
        30,
    )
    assert stacked.predecessors == (source_job.id, code_job.id)
    assert stacked.base == f"work/{code_job.id[:8]}"
    with pytest.raises(ValueError, match="unknown predecessors: msg-1:missing"):
        jobs.submit(_code_payload(tmp_path, "msg-1:bad1", predecessors=["msg-1:missing"]), 40)
    with pytest.raises(ValueError, match="unknown receipt key in base: @msg-1:missing"):
        jobs.submit(_code_payload(tmp_path, "msg-1:bad2", base="@msg-1:missing"), 40)
    with pytest.raises(ValueError, match="not a code job"):
        jobs.submit(_code_payload(tmp_path, "msg-1:bad3", base="@msg-1:src"), 40)


# --- max_reviews and the review cycle -------------------------------------------------------


def host_config(root: Path, **extra) -> Path:
    assert main(["--root", str(root), "init"]) == 0
    path = root / "taskwatch" / "jobs-config.json"
    data = {**json.loads(path.read_text(encoding="utf-8")), **extra}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_init_writes_the_default_and_the_loader_reads_it(tmp_path):
    root = tmp_path / "root"
    path = host_config(root)
    assert json.loads(path.read_text(encoding="utf-8"))["max_reviews"] == 3
    assert load_job_config(root).max_reviews == 3


def test_max_reviews_is_per_job_over_host_over_built_in_and_frozen_at_submit(tmp_path):
    root = tmp_path / "root"
    jobs = Jobs(root)
    assert jobs.submit(payload(tmp_path, "a"), 1).max_reviews == 3  # before any host config
    path = host_config(root, max_reviews=2)
    assert jobs.submit(payload(tmp_path, "b"), 2).max_reviews == 2
    assert jobs.submit({**payload(tmp_path, "c"), "max_reviews": 1}, 3).max_reviews == 1
    path.write_text(
        json.dumps({**json.loads(path.read_text(encoding="utf-8")), "max_reviews": 3}),
        encoding="utf-8",
    )
    stored = {job.receipt_key: job.max_reviews for job in Jobs(root).load().values()}
    assert stored == {"a": 3, "b": 2, "c": 1}  # a later host change leaves a stored job alone


BAD_COUNTS = [True, None, "2", 2.0, 0, 4, -1, [2]]


@pytest.mark.parametrize("value", BAD_COUNTS)
def test_an_invalid_per_job_max_reviews_is_refused_before_anything_is_stored(tmp_path, value):
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError, match="max_reviews"):
        jobs.submit({**payload(tmp_path), "max_reviews": value}, 1)
    assert jobs.load() == {}
    assert not any((jobs.state.dir / "jobs").glob("*"))


@pytest.mark.parametrize("value", BAD_COUNTS)
def test_an_invalid_host_max_reviews_is_a_config_error_and_blocks_submission(tmp_path, value):
    root = tmp_path / "root"
    host_config(root, max_reviews=value)
    with pytest.raises(ValueError, match="max_reviews"):
        load_job_config(root)
    with pytest.raises(ValueError, match="max_reviews"):
        Jobs(root).submit(payload(tmp_path), 1)
    assert Jobs(root).load() == {}


def parked(tmp_path, **changes):
    jobs = Jobs(tmp_path / "root")
    job = jobs.submit(payload(tmp_path), 10)
    job = replace(job, **changes)
    jobs.save(job, 11)
    return jobs, job


def test_resume_opens_a_fresh_cycle_and_keeps_every_record(tmp_path):
    history = ({"round": 1, "seq": 1, "ok": False}, {"round": 2, "seq": 2, "ok": False})
    jobs, job = parked(
        tmp_path,
        stage="user_wait",
        wait_reason="review_exhausted",
        resume_stage="fix",
        reviews=history,
        reviews_used=3,
        recoveries=2,
        review_inflight=4,
        retry_not_before=99.0,
    )
    assert main(["--root", str(jobs.state.root), "resume", job.id, "--reason", "again"]) == 0
    current = jobs.load()[job.id]
    assert (current.stage, current.resume_stage, current.recoveries) == ("queued", "fix", 0)
    assert (current.reviews_used, current.review_cycle_start, current.review_inflight) == (0, 2, 0)
    assert current.reviews == history and current.events[-1]["type"] == "resume"
    assert current.retry_not_before == 99.0  # a resume never skips a rate-limit deadline


def test_approval_waits_keep_the_review_cycle(tmp_path):
    jobs, job = parked(
        tmp_path, stage="review", reviews_used=2, review_cycle_start=1, reviews=({"round": 1},) * 2
    )
    root = str(jobs.state.root)
    assert main(["--root", root, "wait", job.id, "--reason", "needs a gate"]) == 0
    assert main(["--root", root, "approve", job.id, "--reason", "ok"]) == 0
    current = jobs.load()[job.id]
    assert (current.stage, current.reviews_used, current.review_cycle_start) == ("queued", 2, 1)

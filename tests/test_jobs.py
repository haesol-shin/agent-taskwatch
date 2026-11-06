import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch.job_cli import main
from taskwatch.jobs import Jobs, decode_job, load_job_config
from taskwatch.state import RecordError
from tests.git_fixture import git, make_repo
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
    assert second.base_predecessor == first.id
    assert third.predecessors == (first.id, second.id)
    assert third.base == f"taskwatch/{second.id[:8]}"
    reloaded = jobs.load()
    assert reloaded[second.id].predecessors == (first.id,)
    assert reloaded[second.id].base == f"taskwatch/{first.id[:8]}"
    assert reloaded[second.id].base_predecessor == first.id


@pytest.mark.parametrize("stage", ["deliver_pending", "done"])
def test_receipt_submission_compares_public_text_with_landed_commit_not_retained_branch(
    tmp_path, stage
):
    repo = make_repo(tmp_path)
    jobs = Jobs(tmp_path / "root")
    predecessor = jobs.submit(
        _code_payload(
            tmp_path,
            "predecessor",
            target=str(repo),
            land=True,
            public_text=[{"id": "result", "base": "HEAD"}],
        )
    )
    parent = git(repo, "rev-parse", "HEAD")
    (repo / "out.txt").write_text("approved result\n", encoding="utf-8")
    git(repo, "commit", "-am", "approved result")
    reviewed = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    landed = git(repo, "commit-tree", tree, "-p", parent, "-m", "landed result")
    branch = "taskwatch/predecessor"
    git(repo, "branch", branch, reviewed)
    jobs.save(
        replace(
            predecessor,
            stage=stage,
            branch=branch,
            land_state={
                "S": landed,
                "T": tree,
                "tree_verified": True,
                "install": {"status": "disabled"},
            },
        ),
        2000,
    )
    successor = jobs.submit(
        _code_payload(
            tmp_path,
            "successor",
            target=str(repo),
            base="@predecessor",
            public_text=[{"id": "result", "base": landed}],
        )
    )
    assert reviewed != landed
    assert git(repo, "rev-parse", branch) == reviewed
    assert successor.base == landed
    assert successor.base_predecessor == predecessor.id
    assert successor.public_text[0]["base"] == landed
    with pytest.raises(ValueError, match="effective job base"):
        jobs.submit(
            _code_payload(
                tmp_path,
                "partial-successor",
                target=str(repo),
                base="@predecessor",
                public_text=[{"id": "result", "base": reviewed}],
            )
        )


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
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "worker_mode" not in data and "worker_command" not in data
    assert load_job_config(root).worker_mode == "rpc"
    assert load_job_config(root).worker_command is None


def test_private_tmp_is_on_when_the_init_draft_omits_it(tmp_path):
    root = tmp_path / "root"
    path = host_config(root)
    assert "private_tmp" not in json.loads(path.read_text(encoding="utf-8"))
    assert load_job_config(root).private_tmp is True


def test_private_tmp_can_be_turned_off(tmp_path):
    root = tmp_path / "root"
    host_config(root, private_tmp=False)
    assert load_job_config(root).private_tmp is False


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_private_tmp_that_is_not_a_boolean_is_a_config_error(tmp_path, value):
    root = tmp_path / "root"
    host_config(root, private_tmp=value)
    with pytest.raises(ValueError, match="private_tmp"):
        load_job_config(root)


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


def test_landing_defaults_history_and_frozen_state(tmp_path):
    job = Jobs(tmp_path / "root").submit(payload(tmp_path))
    assert job.land is False and dict(job.land_state) == {} and job.base_predecessor == ""
    data = job.to_json()
    for key in ("land", "land_state", "base_predecessor"):
        del data[key]
    assert decode_job(data).land is False
    saved = replace(job, land_state={"install": {"status": "disabled"}, "runs": [1]})
    with pytest.raises(TypeError):
        saved.land_state["install"]["status"] = "succeeded"
    assert saved.land_state["runs"] == (1,)
    assert saved.to_json()["land_state"] == {"install": {"status": "disabled"}, "runs": [1]}


@pytest.mark.parametrize("value", [0, 1, None, "true", [], {}])
def test_landing_rejects_nonboolean_submission(tmp_path, value):
    with pytest.raises(ValueError, match="land must be boolean"):
        Jobs(tmp_path / "root").submit({**payload(tmp_path), "land": value})


def test_landing_requires_building_code_and_publication(tmp_path):
    jobs = Jobs(tmp_path / "root")
    for data in (
        {**payload(tmp_path), "land": True},
        _code_payload(tmp_path, "code", land=True),
        {
            **_code_payload(tmp_path, "review", land=True),
            "review_target": {"files": ["intent.md"]},
        },
    ):
        if "review_target" in data:
            for key in ("outputs", "check_command", "checklist", "progress"):
                del data[key]
        with pytest.raises(ValueError, match="land requires"):
            jobs.submit(data)


def test_landing_accepts_caller_assertion_without_checked_marker(tmp_path):
    repo = make_repo(tmp_path, "publication")
    data = _code_payload(
        tmp_path,
        "land",
        target=str(repo),
        land=True,
        approval_scope="Approved scope assertion without special keywords",
        public_text=[{"id": "result", "base": "HEAD"}],
    )
    job = Jobs(tmp_path / "root").submit(data)
    assert job.land and job.public_text[0]["base"] == git(repo, "rev-parse", "HEAD")
    assert not job.texts and not job.land_state
    with pytest.raises(ValueError, match="unknown fields"):
        Jobs(tmp_path / "other").submit({**data, "land_state": {"S": "a" * 40}})


@pytest.mark.parametrize("key", ["approval_scope", "approval_source"])
def test_landing_requires_nonempty_approval_records(tmp_path, key):
    data = _code_payload(tmp_path, "land", land=True)
    data[key] = " "
    with pytest.raises(ValueError, match=key):
        Jobs(tmp_path / "root").submit(data)


@pytest.mark.parametrize(
    "command",
    [
        [],
        "wrapper {sha}",
        ["wrapper"],
        ["wrapper", "{sha:.8}"],
        ["wrapper", "{sha!r}"],
        ["wrapper", "{sha[0]}"],
        ["wrapper", "{other}"],
        ["wrapper", "{{sha}}"],
        ["sh", "-c", "wrapper {sha}"],
    ],
)
def test_host_install_command_rejects_unpinned_or_interpreted_templates(tmp_path, command):
    root = tmp_path / "root"
    assert main(["--root", str(root), "init"]) == 0
    path = root / "taskwatch" / "jobs-config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["install_command"] = command
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="install_command"):
        load_job_config(root)


@pytest.mark.parametrize(
    "command",
    [
        None,
        ["wrapper", "{sha}", "{target}"],
        ["installer", "git+https://forge.invalid/owner/repo.git@{sha}"],
    ],
)
def test_host_install_command_is_optional_argv(tmp_path, command):
    root = tmp_path / "root"
    assert main(["--root", str(root), "init"]) == 0
    path = root / "taskwatch" / "jobs-config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert load_job_config(root).install_command is None
    config["install_command"] = command
    path.write_text(json.dumps(config), encoding="utf-8")
    assert load_job_config(root).install_command == (tuple(command) if command else None)


@pytest.mark.parametrize(
    "extra,mode,command",
    [
        ({}, "rpc", None),
        ({"worker_mode": "rpc"}, "rpc", None),
        ({"worker_mode": "rpc", "worker_command": ["unused"]}, "rpc", ("unused",)),
        (
            {"worker_mode": "print", "worker_command": ["worker", "{literal}", "--flag"]},
            "print",
            ("worker", "{literal}", "--flag"),
        ),
    ],
)
def test_worker_configuration_preserves_literal_argv_and_review_defaults(
    tmp_path, extra, mode, command
):
    root = tmp_path / "root"
    host_config(root, omp_command=["unavailable-omp.invalid"], **extra)
    config = load_job_config(root)
    assert (config.worker_mode, config.worker_command) == (mode, command)
    assert config.omp_command == ("unavailable-omp.invalid",)
    assert config.review_mode == "rpc"
    assert config.review_command is None


@pytest.mark.parametrize(
    "extra",
    [
        {"worker_mode": "unknown"},
        {"worker_mode": None},
        {"worker_mode": 1},
        {"worker_mode": []},
        {"worker_mode": {}},
        {"worker_mode": "print"},
        {"worker_command": None},
        {"worker_command": []},
        {"worker_command": "worker"},
        {"worker_command": [""]},
        {"worker_command": [" "]},
        {"worker_command": ["worker", ""]},
        {"worker_command": ["worker", 1]},
    ],
)
def test_worker_configuration_rejects_invalid_modes_and_commands(tmp_path, extra):
    root = tmp_path / "root"
    path = host_config(root)
    data = {**json.loads(path.read_text(encoding="utf-8")), **extra}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="worker_(mode|command)"):
        load_job_config(root)


@pytest.mark.parametrize("command", [None, [], "", ["worker", ""], [1]])
def test_print_still_validates_unused_omp_argv(tmp_path, command):
    root = tmp_path / "root"
    path = host_config(root, worker_mode="print", worker_command=["worker"])
    data = {**json.loads(path.read_text(encoding="utf-8")), "omp_command": command}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="omp_command"):
        load_job_config(root)


def test_submission_rejects_a_directory_output_of_the_target_only_for_a_code_job(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    jobs = Jobs(tmp_path / "root")
    with pytest.raises(ValueError, match="output is a directory.*docs"):
        jobs.submit(_code_payload(tmp_path, "dir", target=str(repo), outputs=["docs"]))
    assert jobs.load() == {}
    job = jobs.submit(_code_payload(tmp_path, "file", target=str(repo), outputs=["docs/a.md"]))
    assert job.outputs == ("docs/a.md",)
    # A source or design job writes in its own work folder, so a same-named target folder
    # does not refuse it.
    other = jobs.submit(
        {**payload(tmp_path, "src"), "target": str(repo), "outputs": ["docs"]},
    )
    assert other.outputs == ("docs",)


def _chain(tmp_path):
    """A target repository with a first job `first`, whose branch exists as mid-work."""
    repo = make_repo(tmp_path)
    jobs = Jobs(tmp_path / "root")
    first = jobs.submit(_code_payload(tmp_path, "first", target=str(repo)), 10)
    git(repo, "branch", f"taskwatch/{first.id[:8]}", "HEAD")
    return repo, jobs, first


def test_public_text_base_takes_the_receipt_reference_of_the_job_base(tmp_path):
    repo, jobs, first = _chain(tmp_path)
    second = jobs.submit(
        _code_payload(
            tmp_path,
            "second",
            target=str(repo),
            predecessors=["first"],
            base="@first",
            public_text=[{"id": "pr1", "base": "@first"}],
        ),
        20,
    )
    assert second.base_predecessor == first.id
    # Workspace creation resolves it together with the job base.
    assert second.public_text[0]["base"] == "@first"
    assert jobs.load()[second.id].public_text[0]["base"] == "@first"


@pytest.mark.parametrize("job_base", [None, "@other", "HEAD"])
def test_public_text_receipt_base_must_equal_the_job_base(tmp_path, job_base):
    repo, jobs, _ = _chain(tmp_path)
    jobs.submit(_code_payload(tmp_path, "other", target=str(repo)), 15)
    extra = {} if job_base is None else {"base": job_base}
    with pytest.raises(ValueError, match="must equal the job base"):
        jobs.submit(
            _code_payload(
                tmp_path,
                "second",
                target=str(repo),
                public_text=[{"id": "pr1", "base": "@first"}],
                **extra,
            )
        )


def test_public_text_ref_that_cannot_match_a_running_predecessor_is_rejected(tmp_path):
    repo, jobs, first = _chain(tmp_path)
    # The predecessor's branch resolves now, but its tip moves until the job ends.
    for ref in ("HEAD", f"taskwatch/{first.id[:8]}"):
        with pytest.raises(ValueError, match="same @<receipt key> as base"):
            jobs.submit(
                _code_payload(
                    tmp_path,
                    "second",
                    target=str(repo),
                    base="@first",
                    public_text=[{"id": "pr1", "base": ref}],
                ),
                20,
            )
    # A predecessor that has not started has no branch, which the pin says in the same words.
    jobs.submit(_code_payload(tmp_path, "later", target=str(repo)), 30)
    with pytest.raises(ValueError, match="give public_text base the same reference"):
        jobs.submit(
            _code_payload(
                tmp_path,
                "third",
                target=str(repo),
                base="@later",
                public_text=[{"id": "pr1", "base": "taskwatch/not-yet"}],
            ),
            40,
        )


def test_public_text_ref_still_matches_a_finished_nonlanding_predecessor(tmp_path):
    repo, jobs, first = _chain(tmp_path)
    jobs.save(replace(first, stage="deliver_pending", branch=f"taskwatch/{first.id[:8]}"), 15)
    second = jobs.submit(
        _code_payload(
            tmp_path,
            "second",
            target=str(repo),
            base="@first",
            public_text=[{"id": "pr1", "base": "HEAD"}],
        ),
        20,
    )
    assert second.public_text[0]["base"] == git(repo, "rev-parse", "HEAD")

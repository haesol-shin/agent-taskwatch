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


# --- records written before review reservations ---------------------------------------------

NEW_FIELDS = ("max_reviews", "reviews_used", "review_cycle_start", "review_inflight")
MS_429 = "prompt ended error: 429 {} retry-after-ms=1000"


def launch(attempt, stage="review", at=10.0):
    return {"source": "scan", "kind": "launched", "at": at, "attempt": attempt, "stage": stage}


def done(at=11.0):
    return {"type": "review", "at": at, "ok": False, "verdict": "FAIL"}


def old_review(at, round_number=1):
    return {"seq": round_number, "round": round_number, "at": at, "ok": False, "findings": []}


def write_old(path, first, fix_rounds=0, **changes):
    """The job a release before review reservations stored: `fix_rounds` and no new field."""
    data = {k: v for k, v in first.to_json().items() if k not in (*NEW_FIELDS, "retry_not_before")}
    data = {**data, "fix_rounds": fix_rounds, **changes}
    path.write_text(json.dumps({"version": 1, "job": data, "sha256": _digest(data)}) + "\n")


def load_old(tmp_path, fix_rounds=0, **changes):
    jobs, first, _, path = legacy(tmp_path)
    write_old(path, first, fix_rounds, **changes)
    return jobs, jobs.load()[first.id]


def budget(job):
    return (job.reviews_used, job.review_cycle_start, job.review_inflight, job.max_reviews)


def test_a_job_that_never_started_has_used_nothing(tmp_path):
    _, job = load_old(tmp_path)
    assert (job.stage, budget(job), job.retry_not_before) == ("queued", (0, 0, 0, 3), 0.0)
    assert not hasattr(job, "fix_rounds")


@pytest.mark.parametrize(
    ("stage", "attempt", "fix_rounds"),
    [("review", 5, 0), ("fix", 5, 1), ("queued", 5, 0), ("check", 5, 0), ("queued", 0, 1)],
)
def test_empty_history_cannot_prove_an_executed_legacy_cycle(tmp_path, stage, attempt, fix_rounds):
    jobs, job = load_old(tmp_path, fix_rounds, stage=stage, attempt=attempt, events=[], reviews=[])
    assert job.reviews_used == job.max_reviews
    if stage in {"fix", "review"}:
        from taskwatch.job_scan import scan_jobs
        from taskwatch.jobs import JobConfig
        from tests.test_job_scan import FakeService

        service = FakeService()
        assert job.stage == "user_wait" and job.wait_reason == "review_exhausted"
        assert scan_jobs(jobs, JobConfig(), now=20.0, boot_id="synthetic", service=service) == []
        assert service.started == []


@pytest.mark.parametrize("unit", ["", "previous.service"])
def test_an_unexecuted_explicit_resume_proves_a_fresh_empty_cycle(tmp_path, unit):
    events = [
        launch(2, at=5.0),
        done(6.0),
        {"type": "resume", "at": 10.0, "reason": "again"},
    ]
    _, job = load_old(
        tmp_path, events=events, reviews=[old_review(6.0)], stage="queued", attempt=3, unit=unit
    )
    assert (job.stage, budget(job)) == ("queued", (0, 1, 0, 3))


@pytest.mark.parametrize(
    ("stage", "events"),
    [
        ("queued", [{"type": "wait", "at": 2.0}, {"type": "approve", "at": 3.0}]),
        (
            "approval_wait",
            [{"source": "scan", "kind": "approval_wait", "at": 2.0, "attempt": 0}],
        ),
    ],
)
def test_approval_events_do_not_spend_an_unexecuted_legacy_cycle(tmp_path, stage, events):
    _, job = load_old(tmp_path, stage=stage, events=events)
    assert (job.stage, budget(job)) == (stage, (0, 0, 0, 3))


def test_the_cycle_counts_every_launch_since_the_latest_resume_including_interrupted_ones(
    tmp_path,
):
    events = [
        launch(2, at=5.0),
        done(6.0),
        {"type": "resume", "at": 50.0, "reason": "again"},
        launch(4, at=60.0),  # interrupted: no review record
        launch(5, at=70.0),
        done(71.0),
    ]
    reviews = [old_review(6.0, 1), old_review(71.0, 2)]
    _, job = load_old(tmp_path, 1, events=events, reviews=reviews, stage="fix")
    assert (job.stage, budget(job)) == ("fix", (2, 1, 0, 3))


@pytest.mark.parametrize(("stored", "used"), [(3, 3), (1, 1)])
def test_stored_fix_rounds_are_a_lower_bound_not_the_count(tmp_path, stored, used):
    events = [launch(2, at=5.0), done(6.0)]
    reviews = [old_review(6.0)]
    _, job = load_old(tmp_path, stored, events=events, reviews=reviews, stage="fix")
    assert (job.stage, job.reviews_used) == ("fix", used)  # the events prove one review


def test_a_launch_that_ended_in_a_terminal_429_spent_no_review(tmp_path):
    events = [
        launch(2, at=5.0),
        {"type": "step_failed", "at": 6.0, "error": MS_429},
        launch(3, at=7.0),
        {"type": "step_failed", "at": 8.0, "error": "prompt ended error: Unhandled API in x"},
        launch(4, at=9.0),
        done(10.0),
    ]
    _, job = load_old(tmp_path, 1, events=events, reviews=[old_review(10.0)], stage="fix")
    assert job.reviews_used == 2  # the unsupported-API failure is ordinary and still counts


def test_a_running_review_holds_its_reservation(tmp_path):
    _, job = load_old(tmp_path, stage="review", unit="u.service", attempt=3, events=[launch(3)])
    assert (job.stage, budget(job)) == ("review", (1, 0, 3, 3))


@pytest.mark.parametrize("stage", ["fix", "review"])
def test_a_cycle_the_history_cannot_prove_is_used_up_before_another_model_call(tmp_path, stage):
    # Two review records and one launch event: the history is incomplete.
    events = [launch(2, at=5.0), done(6.0)]
    reviews = [old_review(6.0, 1), old_review(7.0, 2)]
    _, job = load_old(tmp_path, 1, events=events, reviews=reviews, stage=stage, unit="u.service")
    assert (job.stage, job.wait_reason, job.resume_stage) == (
        "user_wait",
        "review_exhausted",
        stage,
    )
    assert (job.reviews_used, job.review_cycle_start) == (3, 2)
    assert "cannot be proven" in job.error and len(job.reviews) == 2


def test_an_unproven_job_between_steps_cannot_start_another_review(tmp_path):
    reviews = [old_review(6.0, 1)]
    _, job = load_old(tmp_path, 1, events=[], reviews=reviews, stage="check")
    assert (job.stage, job.reviews_used) == ("check", 3)


def test_a_running_review_without_its_launch_event_is_unproven(tmp_path):
    _, job = load_old(tmp_path, stage="review", unit="u.service", attempt=3, events=[])
    assert (job.stage, job.reviews_used) == ("user_wait", 3)


@pytest.mark.parametrize("running", [False, True])
def test_a_later_launch_cannot_account_for_an_earlier_review(tmp_path, running):
    events = [launch(3, at=20.0)]
    if not running:
        events.append(done(21.0))
    _, job = load_old(
        tmp_path,
        reviews=[old_review(6.0)],
        events=events,
        stage="review" if running else "fix",
        unit="u.service" if running else "",
        attempt=3,
    )
    assert (job.stage, job.wait_reason, job.reviews_used) == (
        "user_wait",
        "review_exhausted",
        3,
    )


def test_the_decode_is_deterministic_and_rewrites_nothing(tmp_path):
    events = [launch(2), done()]
    jobs, job = load_old(tmp_path, 1, events=events, reviews=[old_review(11.0)], stage="fix")
    stored = {p: p.read_bytes() for p in jobs.state.dir.rglob("state.json")}
    assert jobs.load()[job.id] == job == Jobs(jobs.state.root).load()[job.id]
    assert {p: p.read_bytes() for p in jobs.state.dir.rglob("state.json")} == stored
    assert "fix_rounds" in next(iter(stored.values())).decode()  # the stored record is untouched


def test_a_later_host_setting_does_not_change_an_older_cycle(tmp_path):
    jobs, first, _, path = legacy(tmp_path)
    (jobs.state.root / "taskwatch" / "jobs-config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "slots": 1,
                "stall_seconds": 60,
                "omp_command": ["omp"],
                "rpc_timeout": 60,
                "deliver_command": ["send", "{message}"],
                "max_reviews": 1,
            }
        )
    )
    write_old(path, first)
    assert jobs.load()[first.id].max_reviews == 3
    assert Jobs(jobs.state.root).submit(payload(tmp_path, "message:3"), 5).max_reviews == 1


def test_an_older_record_without_fix_rounds_still_loads(tmp_path):
    jobs, first, _, path = legacy(tmp_path)
    data = {k: v for k, v in first.to_json().items() if k not in NEW_FIELDS}
    path.write_text(json.dumps({"version": 1, "job": data, "sha256": _digest(data)}) + "\n")
    assert budget(jobs.load()[first.id]) == (0, 0, 0, 3)


def test_the_old_and_new_budget_fields_never_mix(tmp_path):
    jobs, first, _, path = legacy(tmp_path)
    data = {**first.to_json(), "fix_rounds": 1}
    path.write_text(json.dumps({"version": 1, "job": data, "sha256": _digest(data)}) + "\n")
    with pytest.raises(RecordError):
        jobs.load()

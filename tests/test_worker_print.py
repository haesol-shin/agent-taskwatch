"""Neutral workers exercised with synthetic, real subprocesses and local delivery."""

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_cli, job_runner
from taskwatch.job_cli import main
from taskwatch.job_runner import run_job
from taskwatch.jobs import load_job_config
from tests.git_fixture import make_repo
from tests.test_job_delivery import Sender
from tests.test_job_runner import GOOD_FILES, Env
from tests.test_job_scan import Board
from tests.test_jobs import host_config
from tests.test_review_print import ANSWER, FINDING

FINDING_TEXT = "The report omits the required reviewed marker."
FINAL_OUTPUT = "ok\nreviewed\n"
WORKER = r"""
import json, os, sys
from pathlib import Path
settings = json.loads(sys.argv[1])
prompt = sys.stdin.read()
folder = Path.cwd().parent
progress = folder / 'PROGRESS.md'
prior_progress = progress.read_text() if progress.exists() else None
fix = 'This is a fix round.' in prompt
for name, text in settings['files'].items():
    root = Path.cwd() if name == 'out.txt' else folder
    (root / name).write_text(text, encoding='utf-8')
if fix:
    (Path.cwd() / 'out.txt').write_text('ok\nreviewed\n', encoding='utf-8')
    (folder / 'findings.json').write_text(
        json.dumps([{'id': 'r1.F1', 'status': 'fixed'}]), encoding='utf-8')
with open(settings['audit'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps({'prompt': prompt, 'pid': os.getpid(), 'argv': sys.argv,
                            'cwd': os.getcwd(), 'marker': os.environ['WORKER_MARKER'],
                            'prior_progress': prior_progress,
                            'workflow': (folder / 'WORKFLOW.md').read_text()}) + '\n')
print('{"verdict":"PASS","blocking":false}')
print('worker diagnostic', file=sys.stderr)
"""
REVIEWER = r"""
import json, sys
from pathlib import Path
settings = json.loads(sys.argv[1])
prompt = sys.stdin.read()
actual = (Path.cwd() / 'out.txt').read_text(encoding='utf-8')
reply = settings['pass'] if actual == 'ok\nreviewed\n' else settings['fail']
print(json.dumps(reply))
print('independent reviewer diagnostic', file=sys.stderr)
"""


def command(script: str, **settings) -> tuple[str, ...]:
    return (sys.executable, "-c", script, json.dumps(settings))


def print_env(tmp_path, monkeypatch, script=WORKER, **settings):
    env = Env(tmp_path, monkeypatch)
    settings = {"files": GOOD_FILES, "audit": str(tmp_path / "worker.jsonl"), **settings}
    env.config = replace(
        env.config,
        worker_mode="print",
        worker_command=command(script, **settings),
        omp_command=("unavailable-omp.invalid",),
        env={"WORKER_MARKER": "synthetic"},
    )
    return env


def audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_standalone_submit_show_scan_run_fix_review_and_exactly_once_delivery(
    tmp_path, monkeypatch, capsys
):
    board = Board(tmp_path)
    board.clock = time.time()
    root = board.jobs.state.root
    sender = Sender(tmp_path / "sender")
    sender.modes("success")
    audit_path = tmp_path / "worker.jsonl"
    finding = {**FINDING, "severity": "P1", "summary": FINDING_TEXT}
    failure = {**ANSWER, "verdict": "FAIL", "blocking": True, "findings": [finding]}
    host_config(
        root,
        omp_command=["unavailable-omp.invalid"],
        worker_mode="print",
        worker_command=list(command(WORKER, files=GOOD_FILES, audit=str(audit_path))),
        review_mode="print",
        review_command=list(command(REVIEWER, **{"pass": ANSWER, "fail": failure})),
        deliver_command=list(sender.command),
        env={"WORKER_MARKER": "synthetic", "PATH": str(tmp_path / "no-executables")},
    )
    board.config = load_job_config(root)
    monkeypatch.setattr(job_cli, "_wake", lambda root: None)
    monkeypatch.setitem(sys.modules, "hermes", None)

    def forbidden(*args, **kwargs):
        pytest.fail("A print-only job must not enter the omp RPC path")

    monkeypatch.setattr(job_runner, "_converse", forbidden)
    predecessor = board.add(review="source")
    pred_folder = board.jobs.folder(board.get(predecessor))
    pred_work = pred_folder / "work"
    pred_work.mkdir()
    (pred_work / "out.txt").write_text("prerequisite evidence\n", encoding="utf-8")
    board.set(predecessor, stage="done", workspace=str(pred_work), fingerprint="prior-evidence")
    work_input = tmp_path / "input.txt"
    work_input.write_text("immutable worker evidence\n", encoding="utf-8")
    data = board.payload(
        review="source",
        prompt="Produce the synthetic report with its reviewed marker.",
        predecessors=[predecessor],
        checklist="checklist.json",
        progress="PROGRESS.md",
        inputs={"work": [str(work_input)]},
        check_command=[
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('out.txt').read_text().startswith('ok\\n')",
        ],
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert main(["--root", str(root), "submit", str(manifest)]) == 0
    identifier = json.loads(capsys.readouterr().out.splitlines()[-1])["id"]
    assert main(["--root", str(root), "show", identifier]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["stage"] == "queued" and shown["predecessors"] == [predecessor]
    stages = ["work", "check", "review", "fix", "check", "review"]
    outcomes = ["check", "review", "fix", "check", "review", "deliver_pending"]
    attempts = []
    for stage, outcome in zip(stages, outcomes, strict=True):
        assert board.scan() == [identifier]
        current = board.get(identifier)
        assert current.stage == stage
        attempts.append(current.attempt)
        assert (
            main(["--root", str(root), "run", identifier, "--attempt", str(current.attempt)]) == 0
        )
        assert board.get(identifier).stage == outcome
        board.service.states[current.unit] = "inactive"
    result = board.get(identifier)
    folder = board.jobs.folder(result)
    assert Path(result.workspace, "out.txt").read_text() == FINAL_OUTPUT
    assert [record["ok"] for record in result.checks] == [True, True]
    assert [record["verdict"] for record in result.reviews] == ["FAIL", "PASS"]
    assert result.reviews[0]["findings"][0]["summary"] == FINDING_TEXT
    assert result.reviews[-1]["fingerprint"] == result.fingerprint
    assert result.reviews[-1]["basis"] == result.basis
    launches = [event for event in result.events if event.get("type") == "worker_launch"]
    assert [(event["stage"], event["mode"]) for event in launches] == [
        ("work", "print"),
        ("fix", "print"),
    ]
    assert [event["attempt"] for event in launches] == [attempts[0], attempts[3]]
    assert all(event["argv"][0] == event["executable"] == sys.executable for event in launches)
    assert result.session_path == result.session_dir == ""
    calls = audit(audit_path)
    assert len(calls) == 2 and calls[0]["pid"] != calls[1]["pid"]
    for call in calls:
        assert call["cwd"] == result.workspace and call["marker"] == "synthetic"
        assert call["prompt"].startswith(data["prompt"])
        assert result.approval_source in call["prompt"]
        assert result.approval_scope in call["prompt"]
        assert str(pred_work / "out.txt") in call["prompt"]
        assert "prior-evidence" in call["prompt"]
        assert result.inputs["work"][0] in call["prompt"]
        assert Path(result.inputs["work"][0]).read_text() == "immutable worker evidence\n"
        assert "--continue" not in call["argv"] and "--last" not in call["argv"]
    assert FINDING_TEXT not in calls[0]["prompt"]
    assert result.reviews[0]["body"] in calls[1]["prompt"]
    assert "r1.F1" in calls[1]["prompt"] and "This is a fix round." in calls[1]["prompt"]
    for event in launches:
        prefix = folder / "logs" / f"{event['stage']}-a{event['attempt']}"
        assert "PASS" in prefix.with_suffix(".stdout").read_text()
        assert prefix.with_suffix(".stderr").read_text() == "worker diagnostic\n"
    assert sender.calls() == []
    assert board.scan() == []
    done = board.get(identifier)
    assert done.stage == "done"
    assert [delivery["status"] for delivery in done.deliveries] == ["success"]
    assert len(sender.calls()) == 1
    assert sender.calls()[0]["args"][1] == "person.invalid"
    assert board.scan() == []
    assert board.get(identifier) == done and len(sender.calls()) == 1


@pytest.mark.parametrize("problem", ["missing_output", "bad_checklist", "failed_check"])
def test_stdout_pass_cannot_bypass_existing_output_and_check_gates(tmp_path, monkeypatch, problem):
    files = dict(GOOD_FILES)
    if problem == "missing_output":
        del files["out.txt"]
    elif problem == "bad_checklist":
        files["checklist.json"] = '{"items": [{"id": "a", "passes": false}]}'
    else:
        files["out.txt"] = "wrong\n"
    env = print_env(tmp_path, monkeypatch, files=files)
    assert env.run("work") == 0 and env.get().stage == "check"
    assert env.run("check") == 0
    assert env.get().stage == "fix"
    assert not env.get().checks[-1]["ok"] and not env.get().reviews


@pytest.mark.parametrize(
    "failure", ["missing", "nonzero", "signal", "timeout", "feed_timeout", "closed_stdin"]
)
def test_worker_failures_retain_logs_without_fallback_or_check_advancement(
    tmp_path, monkeypatch, failure
):
    script = "import sys; sys.stdin.read(); print('raw stdout'); "
    if failure == "nonzero":
        script += "sys.stderr.write('diagnostic-' * 2000); sys.exit(7)"
    elif failure == "signal":
        script += "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    elif failure == "timeout":
        script += "import time; time.sleep(30)"
    elif failure == "feed_timeout":
        script = "import time; time.sleep(30)"
    else:
        script = "import os; os.close(0); print('PASS', flush=True)"
    env = print_env(tmp_path, monkeypatch, script)
    if failure == "missing":
        env.config = replace(env.config, worker_command=("missing-worker.invalid",))
    if failure in {"timeout", "feed_timeout", "closed_stdin"}:
        env.config = replace(env.config, rpc_timeout=0.3)
    if failure in {"feed_timeout", "closed_stdin"}:
        env.put("work", prompt="large prompt " * 100000)
        code = run_job(env.jobs, env.config, env.job.id, env.attempt)
    else:
        code = env.run("work")
    assert code == 1
    current = env.get()
    assert current.stage == "work" and current.ended_at == 0
    assert not current.checks and not current.reviews
    assert current.error and len(current.error) < 6000
    assert current.session_path == current.session_dir == ""
    assert not env.state.exists()
    logs = env.folder() / "logs"
    assert list(logs.glob("work-*.stdout")) and list(logs.glob("work-*.stderr"))
    if failure == "nonzero":
        assert "diagnostic-" in current.error
        assert next(logs.glob("work-*.stderr")).stat().st_size == 22000
        assert next(logs.glob("work-*.stdout")).read_text() == "raw stdout\n"


def test_recovery_is_fresh_and_records_changed_argv_without_losing_progress(tmp_path, monkeypatch):
    script = (
        "import pathlib, sys; sys.stdin.read(); "
        "pathlib.Path('../PROGRESS.md').write_text('unfinished evidence'); sys.exit(2)"
    )
    env = print_env(tmp_path, monkeypatch, script)
    assert env.run("work") == 1
    first = env.get()
    env.config = replace(
        env.config,
        worker_command=command(WORKER, files=GOOD_FILES, audit=str(tmp_path / "worker.jsonl")),
    )
    assert env.run("work", recoveries=1) == 0
    current = env.get()
    events = [event for event in current.events if event["type"] == "worker_launch"]
    assert len(events) == 2 and events[0]["argv"] != events[1]["argv"]
    assert events[0]["attempt"] == first.attempt
    invocation = audit(tmp_path / "worker.jsonl")[0]
    assert invocation["prior_progress"] == "unfinished evidence"
    prompt = invocation["prompt"]
    assert "You were interrupted." in prompt and str(env.folder() / "PROGRESS.md") in prompt
    assert (
        current.workspace == first.workspace and current.session_path == current.session_dir == ""
    )


def test_printed_completion_is_not_exit_and_stale_attempt_cannot_advance(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    script = (
        "import pathlib, sys, time; sys.stdin.read(); print('PASS', flush=True); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    env = print_env(tmp_path, monkeypatch, script)
    monkeypatch.setattr(job_runner, "WATCH_EVERY", 0.01)
    env.put("work")
    result = []
    worker = threading.Thread(
        target=lambda: result.append(run_job(env.jobs, env.config, env.job.id, env.attempt))
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists() and worker.is_alive()
        running = env.get()
        assert running.stage == "work" and not running.ended_at and not running.checks
        with env.jobs.state.lock():
            replaced = replace(env.get(), attempt=running.attempt + 1, stage="cancelled")
            env.jobs.save(replaced, time.time())
        worker.join(3)
        assert not worker.is_alive() and result == [2]
        assert env.get() == replaced
    finally:
        worker.join(6)


@pytest.mark.parametrize("change", ["output", "basis"])
def test_stale_evidence_cannot_deliver_even_when_print_reviewer_says_pass(
    tmp_path, monkeypatch, change
):
    env = print_env(tmp_path, monkeypatch)
    env.config = replace(
        env.config,
        review_mode="print",
        review_command=command(
            "import json, sys; sys.stdin.read(); print(json.loads(sys.argv[1])['reply'])",
            reply=json.dumps(ANSWER),
        ),
    )
    assert env.run("work") == 0
    assert env.run("check") == 0 and env.get().stage == "review"
    if change == "output":
        (Path(env.get().workspace) / "out.txt").write_text("unreviewed\\n", encoding="utf-8")
    else:
        guideline = env.folder() / "review-docs" / "source.md"
        guideline.write_text(guideline.read_text() + "\nChanged review basis.\n", encoding="utf-8")
    assert env.run("review") == (1 if change == "basis" else 0)
    current = env.get()
    assert current.stage != "deliver_pending"
    assert not any(record["ok"] for record in current.reviews)


@pytest.mark.parametrize("staged", [False, True])
def test_print_worker_cannot_pass_dirty_or_staged_uncommitted_code(tmp_path, monkeypatch, staged):
    env = Env(tmp_path, monkeypatch, review="code", target=make_repo(tmp_path))
    script = WORKER
    if staged:
        script += "\nimport subprocess; subprocess.run(['git', 'add', 'out.txt'], check=True)\n"
    env.config = replace(
        env.config,
        worker_mode="print",
        worker_command=command(script, files=GOOD_FILES, audit=str(tmp_path / "worker.jsonl")),
        env={"WORKER_MARKER": "synthetic"},
    )
    assert env.run("work") == 0
    assert env.run("check") == 0
    current = env.get()
    assert current.stage == "fix" and not current.checks[-1]["ok"]
    assert not current.reviews


@pytest.mark.parametrize("historical", [False, True])
def test_existing_rpc_attempt_cannot_adopt_print_but_new_attempt_can(
    tmp_path, monkeypatch, historical
):
    from taskwatch.jobs import decode_job
    from tests.test_job_runner import work_turn

    env = Env(tmp_path, monkeypatch)
    env.scenario([{"die_after_content": True}, work_turn()])
    assert env.run("work") == 1
    prior = env.get()
    events = prior.events
    if historical:
        events = tuple(
            {key: value for key, value in event.items() if key != "attempt"}
            for event in events
            if event["type"] != "worker_launch"
        )
        data = replace(prior, events=events).to_json()
        data.pop("legacy_worker_attempt")
        prior = decode_job(data)
    env.jobs.save(replace(prior, ended_at=0, events=events), time.time())
    env.config = replace(
        env.config,
        worker_mode="print",
        worker_command=command(WORKER, files=GOOD_FILES, audit=str(tmp_path / "worker.jsonl")),
        env={"WORKER_MARKER": "synthetic"},
    )
    assert run_job(env.jobs, env.config, prior.id, prior.attempt) == 0
    resumed = env.get()
    assert resumed.session_path == prior.session_path
    assert not (tmp_path / "worker.jsonl").exists()
    assert [event["resumed"] for event in resumed.events if event["type"] == "session_open"] == [
        False,
        True,
    ]
    assert env.run("work", started_at=time.time()) == 0
    current = env.get()
    assert current.session_path == current.session_dir == ""
    assert audit(tmp_path / "worker.jsonl")[0]["cwd"] == current.workspace
    assert current.events[-2]["type"] == "worker_launch"
    assert current.events[-2]["mode"] == "print"


def test_same_print_attempt_pins_command_and_recovery_reminder(tmp_path, monkeypatch):
    env = print_env(tmp_path, monkeypatch)
    env.put("work")
    ctx = job_runner._context(env.jobs, env.config, env.get())
    job_runner._worker_identity(ctx)
    pinned = env.get()
    changed = replace(env.config, worker_mode="rpc", omp_command=("missing-rpc.invalid",))
    assert run_job(env.jobs, changed, pinned.id, pinned.attempt) == 0
    assert env.get().stage == "check" and env.get().session_path == ""
    invocation = audit(tmp_path / "worker.jsonl")[0]
    assert "You were interrupted." in invocation["prompt"]
    assert len([event for event in env.get().events if event["type"] == "worker_launch"]) == 1


def test_evidence_heartbeat_ignores_logs_but_tracks_progress_and_is_cas_fenced(
    tmp_path, monkeypatch
):
    env = print_env(tmp_path, monkeypatch)
    env.put("work")
    env.seed(GOOD_FILES)
    ctx = job_runner._context(env.jobs, env.config, env.get())
    recorder = job_runner.Recorder(ctx)
    recorder.beat()
    initial = env.get()
    (env.folder() / "logs" / "work-a1.stdout").write_text("PASS\n" * 10000)
    recorder.beat()
    heartbeat = env.get()
    assert heartbeat.evidence_at > initial.evidence_at
    assert heartbeat.last_progress_at == initial.last_progress_at
    assert heartbeat.progress_hash == initial.progress_hash
    (env.folder() / "PROGRESS.md").write_text("real progress\n")
    recorder.beat()
    progressed = env.get()
    assert progressed.progress_hash != initial.progress_hash
    assert progressed.last_progress_at > initial.last_progress_at
    replaced = replace(progressed, attempt=progressed.attempt + 1)
    env.jobs.save(replaced, time.time())
    recorder.beat()
    assert recorder.abort.is_set() and env.get() == replaced


def test_stale_replacement_cancels_blocked_prompt_feed(tmp_path, monkeypatch):
    ready = tmp_path / "feed-ready"
    env = print_env(
        tmp_path,
        monkeypatch,
        f"import pathlib, signal; pathlib.Path({str(ready)!r}).touch(); signal.pause()",
    )
    monkeypatch.setattr(job_runner, "WATCH_EVERY", 0.01)
    env.put("work", prompt="prompt " * 200000)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(run_job(env.jobs, env.config, env.job.id, env.attempt))
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists() and worker.is_alive()
        replaced = replace(env.get(), stage="cancelled", attempt=env.attempt + 1)
        env.jobs.save(replaced, time.time())
        worker.join(3)
        assert not worker.is_alive() and result == [2]
        assert env.get() == replaced
    finally:
        worker.join(6)


def test_legacy_rpc_attempt_before_session_open_keeps_backend(tmp_path, monkeypatch):
    from taskwatch.jobs import decode_job
    from tests.test_job_runner import work_turn

    env = Env(tmp_path, monkeypatch)
    env.put("work")
    data = env.get().to_json()
    data.pop("legacy_worker_attempt")
    assert not data["session_path"] and not data["events"]
    old = decode_job(data)
    env.jobs.save(old, time.time())
    env.scenario([work_turn()])
    changed = replace(
        env.config,
        worker_mode="print",
        worker_command=("must-not-run.invalid",),
    )
    assert run_job(env.jobs, changed, old.id, old.attempt) == 0
    current = env.get()
    assert current.session_path.endswith("s1.jsonl")
    assert current.legacy_worker_attempt == old.attempt
    assert next(e for e in current.events if e["type"] == "worker_launch")["mode"] == "rpc"

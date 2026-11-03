"""Print mode: a review command that answers once, found, validated and parsed by code."""

import json
import os
import re
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from taskwatch import job_runner, review_print
from taskwatch.executor import (
    CATEGORIES,
    DESIGN_KEYS,
    SEVERITIES,
    _design_check,
    _finding_lines,
    _scope,
    _verdict_line,
)
from taskwatch.jobs import load_job_config
from taskwatch.review_docs import docs_dir
from tests.test_job_delivery import write_config
from tests.test_job_runner import Env, work_turn

SCHEMA = json.loads((docs_dir("launch") / review_print.SCHEMA_NAME).read_text(encoding="utf-8"))
ANSWER = {
    "verdict": "PASS",
    "blocking": False,
    "checked": "the diff",
    "not_checked": "runtime",
    "design_check": None,
    "findings": [],
}
FINDING = {
    "finding": "F1",
    "severity": "P2",
    "category": "clarity",
    "rule": "other",
    "location": "out.txt:1",
    "summary": "The output is terse.",
    "detail": "Line one.\n{not a verdict}",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def use(env: Env, reply: str, *extra: str, shell: str | None = None) -> None:
    """A fake reviewer: reads stdin, then prints `reply` (a file it copies)."""
    (env.tmp / "reply.txt").write_text(reply, encoding="utf-8")
    script = shell or f"cat > {env.tmp}/stdin.txt; cat {env.tmp}/reply.txt"
    env.config = replace(
        env.config,
        review_mode="print",
        review_command=("sh", "-c", script, "sh", *extra),
    )


def answer(**changes) -> str:
    return json.dumps({**ANSWER, **changes})


# --- the schema and the parser agree ---------------------------------------------------------


def test_schema_enums_are_the_parser_enums():
    finding = SCHEMA["$defs"]["finding"]["properties"]
    assert tuple(finding["severity"]["enum"]) == SEVERITIES
    assert tuple(finding["category"]["enum"]) == CATEGORIES
    assert tuple(SCHEMA["properties"]["design_check"]["required"]) == DESIGN_KEYS
    assert set(SCHEMA["properties"]["design_check"]["properties"]) == set(DESIGN_KEYS)
    # every field of a finding line the parser reads is a field of the schema
    assert {"finding", "severity", "category", "rule", "location", "summary"} <= set(finding)


def test_a_schema_valid_answer_renders_into_what_the_parser_accepts():
    checks = {k: {"answer": "ok", "evidence": f"read {k}"} for k in DESIGN_KEYS}
    full = {**ANSWER, "design_check": checks, "findings": [FINDING]}
    assert review_print.validate(full, SCHEMA) == []
    body = review_print.render(full)
    verdict, blocking = _verdict_line(body)
    assert (verdict, blocking) == ("PASS", False)
    assert _finding_lines(body, verdict)[0]["summary"] == "The output is terse."
    assert set(_design_check(body, [])) == set(DESIGN_KEYS)
    assert _scope(body) == ("the diff", "runtime")
    assert "> {not a verdict}" in body  # free text can never pose as a verdict line


def test_an_empty_summary_and_a_null_design_check_are_left_out_of_the_lines():
    body = review_print.render({**ANSWER, "findings": [{**FINDING, "summary": " "}]})
    assert "design_check" not in body and "summary" not in body
    assert _finding_lines(body, "FAIL")[0]["finding"] == "F1"


@pytest.mark.parametrize(
    "change, fragment",
    [
        ({"verdict": "MAYBE"}, "$.verdict: 'MAYBE' is not one of PASS, FAIL"),
        ({"blocking": "no"}, "$.blocking: expected boolean"),
        ({"extra": 1}, "unexpected extra"),
        ({"findings": [{**FINDING, "severity": "P9"}]}, "$.findings[0].severity"),
        ({"findings": [{**FINDING, "finding": "one"}]}, "$.findings[0].finding"),
        ({"findings": [{k: v for k, v in FINDING.items() if k != "rule"}]}, "missing rule"),
        ({"design_check": {"root_cause": {"answer": "ok", "evidence": "x"}}}, "missing alternat"),
    ],
)
def test_the_validator_names_the_path_of_a_schema_error(change, fragment):
    errors = review_print.validate({**ANSWER, **change}, SCHEMA)
    assert any(fragment in error for error in errors), errors


# --- finding the verdict ---------------------------------------------------------------------


def found(text: str) -> dict:
    return json.loads(review_print.verdict_body([("x", text)], SCHEMA).splitlines()[0])


@pytest.mark.parametrize(
    "text",
    [
        answer(),
        "```json\n" + answer() + "\n```",
        json.dumps({"type": "result", "result": answer()}),  # claude -p --output-format json
        json.dumps({"type": "result", "result": "```json\n" + answer() + "\n```"}),
        json.dumps({"type": "result", "result": "", "structured_output": json.loads(answer())}),
        json.dumps({"response": answer(), "stats": {}}),  # gemini -p --output-format json
        json.dumps({"content": [{"type": "text", "text": answer()}]}),
        '{"type": "start"}\n{"type": "message", "text": ' + json.dumps(answer()) + "}\n",
    ],
)
def test_the_verdict_is_found_in_plain_and_wrapped_output(text):
    assert found(text)["verdict"] == "PASS"


def test_no_verdict_in_the_output_is_named():
    with pytest.raises(review_print.ReplyError, match="no verdict JSON was found in the x"):
        review_print.verdict_body([("x", "looks fine")], SCHEMA)


# --- a job through print mode ----------------------------------------------------------------


def test_a_fake_reviewer_takes_a_job_to_deliver_pending(env):
    use(env, answer())
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    record = job.reviews[-1]
    assert (job.stage, record["ok"], record["reviewer"]) == ("deliver_pending", True, "sh")
    assert (record["checked"], record["not_checked"]) == ("the diff", "runtime")
    sent = (env.tmp / "stdin.txt").read_text(encoding="utf-8")
    assert "independent reviewer" in sent and "ORIGINAL PROMPT TEXT" in sent
    assert f"Verdict schema: {env.folder() / review_print.SCHEMA_NAME}" in sent
    assert "no follow-up in this mode" in sent
    assert json.loads((env.folder() / review_print.SCHEMA_NAME).read_text()) == SCHEMA


def test_print_review_sends_no_rpc_binding_commands(env):
    use(env, answer())
    env.scenario([work_turn()])
    assert env.run("work") == 0
    assert env.run("check") == 0
    assert env.run("review") == 0
    assert env.get().stage == "deliver_pending"
    types = [entry.get("type") for entry in env.log()]
    assert types.count("open_session") == 1  # the work session only


def test_the_last_message_file_is_read_before_stdout(env):
    use(
        env,
        answer(),
        "{output}",
        shell=f"cat > /dev/null; cp {env.tmp}/reply.txt \"$1\"; echo 'progress noise'",
    )
    env.to_review()
    assert env.run("review") == 0
    assert env.get().stage == "deliver_pending"


def test_the_schema_path_reaches_the_command(env):
    use(
        env,
        answer(),
        "{schema}",
        shell=f'cat > /dev/null; cp "$1" {env.tmp}/seen.json; cat {env.tmp}/reply.txt',
    )
    env.to_review()
    assert env.run("review") == 0
    assert json.loads((env.tmp / "seen.json").read_text()) == SCHEMA


def test_findings_of_a_fail_reach_the_fix_session(env):
    finding = {**FINDING, "severity": "P1", "summary": "Wrong."}
    use(env, answer(verdict="FAIL", blocking=True, findings=[finding]))
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    record = job.reviews[-1]
    assert (job.stage, job.reviews_used) == ("fix", 1)
    assert [f["finding"] for f in record["findings"]] == ["F1"]
    assert "> Line one." in record["body"]


def test_a_malformed_verdict_retries_the_review_with_the_schema_error(env):
    use(env, answer(verdict="MAYBE"))
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    record = job.reviews[-1]
    assert (job.stage, job.reviews_used, record["ok"]) == ("review", 1, False)
    assert "does not match the schema" in job.error and "$.verdict" in job.error
    assert "MAYBE" in record["body"]  # the raw reply is kept


def test_a_reply_without_any_verdict_fails_the_round(env):
    use(env, "looks fine to me")
    env.to_review()
    assert env.run("review") == 0
    assert "no verdict JSON was found in the standard output" in env.get().error


def test_a_failing_command_is_a_step_failure_with_its_exit_code(env):
    use(env, "", shell="cat > /dev/null; echo broken >&2; exit 3")
    env.to_review()
    assert env.run("review") == 1
    job = env.get()
    assert (job.stage, job.reviews_used, job.retry_not_before) == ("review", 1, 0.0)
    assert "exited with code 3: broken" in job.error


@pytest.mark.parametrize(
    "response",
    [
        "",
        '{"error":"rate_limit_error","message":"quota exceeded"}',
        '{"error":"rate_limit_error","message":"previous final error: retrying; quota exceeded"}',
    ],
)
def test_an_explicit_http_429_on_standard_error_waits_for_its_deadline(env, response):
    shell = (
        "cat > /dev/null; echo 'HTTP 429 Too Many Requests' >&2;"
        f" echo 'Retry-After: 120' >&2; echo '{response}' >&2; exit 1"
    )
    use(env, "", shell=shell)
    env.to_review()
    before = time.time()
    assert env.run("review") == 1
    job = env.get()
    assert (job.stage, job.reviews_used, job.review_inflight, job.recoveries) == ("review", 0, 0, 0)
    assert before + 120 <= job.retry_not_before <= time.time() + 120
    assert "HTTP 429 Too Many Requests" in job.error


def test_an_explicit_429_without_a_hint_waits_for_the_owner(env):
    use(env, "", shell="cat > /dev/null; echo 'status 429: slow down' >&2; exit 1")
    env.to_review()
    assert env.run("review") == 1
    job = env.get()
    assert (job.stage, job.wait_reason, job.reviews_used) == ("user_wait", "rate_limit", 0)


@pytest.mark.parametrize(
    "shell",
    [
        "cat > /dev/null; echo 'HTTP 429 retry-after-ms=100000'; exit 1",  # standard output only
        "cat > /dev/null; echo 'waited 429 seconds, retry-after-ms=100000' >&2; exit 1",
        "cat > /dev/null; echo 'HTTP 429 Too Many Requests' >&2",  # exits 0: the reply decides
        "cat > /dev/null; echo 'HTTP 429 Retry-After: 120' >&2;"
        " echo 'Unhandled API in mapOptionsForApi: web-search' >&2; exit 1",
        "cat > /dev/null; echo 'HTTP 429 Retry-After: 120' >&2;"
        ' echo \'{"status":500,"error":"Unhandled API in mapOptionsForApi: web-search"}\' >&2;'
        " exit 1",
        "cat > /dev/null; echo 'HTTP 429 Retry-After: 120' >&2;"
        ' echo \'{"error":{"type":"api_error","message":"unsupported API"}}\' >&2; exit 1',
        "cat > /dev/null;"
        " echo 'HTTP 429 Retry-After: 60; final error:"
        " Unhandled API in mapOptionsForApi: web-search'"
        " >&2; exit 1",
        "cat > /dev/null; echo 'HTTP 429 Retry-After: 60 terminal failure: unsupported API'"
        " >&2; exit 1",
    ],
)
def test_generic_mentions_of_429_are_not_a_rate_limit(env, shell):
    use(env, "", shell=shell)
    env.to_review()
    env.run("review")
    job = env.get()
    assert (job.retry_not_before, job.wait_reason, job.reviews_used) == (0.0, "", 1)


def test_a_command_that_never_finishes_is_stopped(env):
    use(env, "", shell="cat > /dev/null; sleep 30")
    env.config = replace(env.config, rpc_timeout=1)
    env.to_review()
    assert env.run("review") == 1
    assert "deadline" in env.get().error


def test_early_closed_review_stdin_cannot_supply_authority(env):
    use(env, answer(), shell=f"exec 0<&-; cat {env.tmp}/reply.txt")
    env.job = replace(env.job, prompt="x" * 1024 * 1024)
    env.jobs.save(env.job, time.time())
    env.to_review()
    assert env.run("review") == 1
    assert env.get().stage == "review"
    assert not env.get().reviews
    assert "stdin" in env.get().error


@pytest.mark.parametrize("phase", ["feed", "wait"])
@pytest.mark.parametrize("replacement", ["cancel", "attempt"])
def test_reviewer_cancellation_and_stale_replacement_stop_real_process(
    env, monkeypatch, phase, replacement
):
    ready = env.tmp / "review-ready"
    script = (
        "import os, pathlib, signal, sys; "
        + ("sys.stdin.read(); " if phase == "wait" else "")
        + f"print({answer()!r}, flush=True); "
        + f"ready = pathlib.Path({str(ready)!r}); "
        + "pending = ready.with_suffix('.tmp'); pending.write_text(str(os.getpid())); "
        + "pending.replace(ready); signal.pause()"
    )
    env.to_review()
    env.config = replace(
        env.config, review_mode="print", review_command=(sys.executable, "-c", script)
    )
    monkeypatch.setattr(job_runner, "WATCH_EVERY", 0.01)
    env.put("review", prompt="prompt " * 200000 if phase == "feed" else env.get().prompt)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            job_runner.run_job(env.jobs, env.config, env.job.id, env.attempt)
        )
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists() and worker.is_alive()
        pid = int(ready.read_text())
        with env.jobs.state.lock():
            current = env.get()
            replaced = replace(
                current,
                stage="cancelled" if replacement == "cancel" else current.stage,
                attempt=current.attempt + (replacement == "attempt"),
            )
            env.jobs.save(replaced, time.time())
        worker.join(3)
        assert not worker.is_alive() and result == [2]
        assert env.get() == replaced
        assert not env.get().reviews
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with env.jobs.state.lock():
            env.jobs.save(replace(env.get(), stage="cancelled"), time.time())
        worker.join(6)


@pytest.mark.parametrize("failure", ["feed-timeout", "wait-timeout", "feed-closed"])
def test_429_before_incomplete_execution_does_not_refund_a_review(env, failure):
    script = (
        "import os, signal, sys; "
        "print('HTTP 429 Too Many Requests; Retry-After: 120', file=sys.stderr, flush=True); "
        + {
            "feed-timeout": "",
            "wait-timeout": "sys.stdin.read(); ",
            "feed-closed": "os.close(0); ",
        }[failure]
        + "signal.pause()"
    )
    env.to_review()
    env.config = replace(
        env.config,
        review_mode="print",
        review_command=(sys.executable, "-c", script),
        rpc_timeout=1,
    )
    changes = {"prompt": "prompt " * 200000} if failure != "wait-timeout" else {}
    assert env.run("review", **changes) == 1
    job = env.get()
    assert (job.stage, job.reviews_used, job.review_inflight) == ("review", 1, 0)
    assert (job.wait_reason, job.retry_not_before) == ("", 0.0)
    assert not job.reviews
    assert ("stdin" if failure == "feed-closed" else "deadline") in job.error
    stderr = env.folder() / "logs" / f"review-a{job.attempt}.stderr"
    assert "HTTP 429" in stderr.read_text()


# --- config ----------------------------------------------------------------------------------


def test_print_mode_needs_a_review_command(tmp_path: Path):
    write_config(tmp_path, review_mode="print")
    with pytest.raises(ValueError, match="needs review_command"):
        load_job_config(tmp_path)
    write_config(tmp_path, review_mode="print", review_command=[sys.executable])
    assert load_job_config(tmp_path).review_mode == "print"


@pytest.mark.parametrize("mode", ["stream", 1, None])
def test_an_unknown_review_mode_is_refused(tmp_path: Path, mode):
    write_config(tmp_path)
    path = tmp_path / "taskwatch" / "jobs-config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["review_mode"] = mode
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="review_mode must be one of"):
        load_job_config(tmp_path)


def test_review_mode_defaults_to_rpc(tmp_path: Path):
    write_config(tmp_path)
    assert load_job_config(tmp_path).review_mode == "rpc"


def test_the_documented_examples_are_accepted_by_the_config_loader(tmp_path: Path):
    text = (Path(__file__).parents[1] / "docs" / "configuration.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(\{.*?\})\n```", text, re.DOTALL)
    examples = [json.loads(b) for b in blocks if '"review_mode"' in b]
    for example in examples:
        write_config(tmp_path, **example)
        config = load_job_config(tmp_path)
        assert config.review_mode == "print" and config.review_command


def test_large_jsonl_reply_stream_keeps_last_verdict_and_raw_evidence(env):
    use(
        env,
        answer(),
        shell=f'cat > /dev/null; yes \'{{"event":"noise"}}\' | head -c 5000000;'
        f" echo; cat {env.tmp}/reply.txt; echo diagnostic >&2",
    )
    env.to_review()
    assert env.run("review") == 0
    job = env.get()
    assert job.stage == "deliver_pending"
    logs = env.folder() / "logs"
    assert (logs / f"review-a{job.attempt}.stdout").stat().st_size > 5000000
    assert (logs / f"review-a{job.attempt}.stderr").read_text().strip() == "diagnostic"


def test_oversized_single_candidate_is_explicitly_rejected(tmp_path):
    path = tmp_path / "reply"
    path.write_text('{"text":"' + "x" * review_print.MAX_REPLY_BYTES + '"}')
    with pytest.raises(review_print.ReplyError, match="candidate exceeds"):
        review_print.verdict_files([("stdout", path)], SCHEMA)


def test_large_jsonl_cannot_override_invalid_last_message_file(tmp_path):
    last = tmp_path / "last"
    last.write_text(answer(verdict="MAYBE"))
    stream = tmp_path / "stdout"
    stream.write_text('{"event":"noise"}\n' * 300000 + answer())
    with pytest.raises(review_print.ReplyError, match="does not match the schema"):
        review_print.verdict_files([("last", last), ("stdout", stream)], SCHEMA)


@pytest.mark.parametrize("fenced", [False, True])
def test_oversized_multiline_wrapper_cannot_invert_wrapper_priority(tmp_path, fenced):
    path = tmp_path / "reply"
    fail = {**ANSWER, "verdict": "FAIL", "blocking": True, "findings": [FINDING]}
    padding = ",\n".join(
        json.dumps("x" * 1000) for _ in range(review_print.MAX_REPLY_BYTES // 1000 + 1)
    )
    text = (
        '{\n"result": '
        + json.dumps(fail)
        + ',\n"padding": [\n'
        + padding
        + '\n],\n"content": [\n'
        + answer()
        + "\n]\n}"
    )
    if fenced:
        text = "```json\n" + text + "\n```"
    path.write_text(text, encoding="utf-8")
    assert path.stat().st_size > review_print.MAX_REPLY_BYTES
    # Whole-document parsing prefers result=FAIL over content=PASS.
    assert found(text)["verdict"] == "FAIL"
    with pytest.raises(review_print.ReplyError, match="candidate exceeds"):
        review_print.verdict_files([("stdout", path)], SCHEMA)


@pytest.mark.parametrize("fenced", [False, True])
def test_large_jsonl_preserves_each_multiline_wrapper_priority(tmp_path, fenced):
    path = tmp_path / "reply"
    fail = {**ANSWER, "verdict": "FAIL", "blocking": True, "findings": [FINDING]}
    with path.open("w", encoding="utf-8") as stream:
        stream.write('{"event":"noise"}\n' * 300000)
        if fenced:
            stream.write("```json\n")
        json.dump({"result": fail, "content": [ANSWER]}, stream, indent=2)
        if fenced:
            stream.write("\n```\n")
    body = review_print.verdict_files([("stdout", path)], SCHEMA)
    assert json.loads(body.splitlines()[0])["verdict"] == "FAIL"

"""The spec template check at submit, and the design_check shape of the verdict line."""

import json
from pathlib import Path

import pytest

from taskwatch.executor import DESIGN_KEYS, _design_check, _finding_lines, _verdict_line
from taskwatch.job_runner import run_job
from taskwatch.jobs import SPEC_HEADINGS, Jobs, spec_sections
from tests.spec_fixture import VALID_SPEC
from tests.test_job_runner import BRIEF, CHOICES, Env
from tests.test_jobs import payload
from tests.verdicts import CODE_FAIL, CODE_PASS, DESIGN_CHECK

ROOT = Path(__file__).resolve().parents[1]

# --- the spec template ---------------------------------------------------------------------


def submit(tmp_path: Path, spec: str, review: str = "code"):
    data = {**payload(tmp_path), "review": review}
    Path(data["spec"]).write_text(spec, encoding="utf-8")
    return Jobs(tmp_path / "root").submit(data, 10)


def spec_without(heading: str) -> str:
    return VALID_SPEC.replace(f"## {heading}\n", "## Renamed\n")


@pytest.mark.parametrize("review", ["code", "design"])
def test_a_filled_spec_is_accepted(tmp_path, review):
    assert submit(tmp_path, VALID_SPEC, review).review == review


@pytest.mark.parametrize("heading", SPEC_HEADINGS)
def test_a_missing_heading_is_rejected_by_name(tmp_path, heading):
    with pytest.raises(ValueError, match=f"missing heading: {heading}"):
        submit(tmp_path, spec_without(heading))
    assert Jobs(tmp_path / "root").load() == {}


@pytest.mark.parametrize("heading", SPEC_HEADINGS)
def test_an_empty_heading_is_rejected_by_name(tmp_path, heading):
    empty = VALID_SPEC.replace(f"## {heading}\n\nn/a: synthetic fixture\n", f"## {heading}\n")
    with pytest.raises(ValueError, match=f"empty heading: {heading}"):
        submit(tmp_path, empty)


def test_na_with_a_reason_passes(tmp_path):
    spec = VALID_SPEC.replace("## Homes\n\nn/a: synthetic fixture", "## Homes\n\nN/A: no new rule")
    assert submit(tmp_path, spec)


@pytest.mark.parametrize("bare", ["n/a", "N/A.", "n/a:", "n/a: "])
def test_a_bare_na_is_rejected(tmp_path, bare):
    text = VALID_SPEC.replace("## Homes\n\nn/a: synthetic fixture", f"## Homes\n\n{bare}")
    with pytest.raises(ValueError, match="needs a reason.*Homes"):
        submit(tmp_path, text)


def test_a_word_that_only_starts_with_na_is_an_answer(tmp_path):
    spec = VALID_SPEC.replace("## Homes\n\nn/a: synthetic fixture", "## Homes\n\nnaming rules: x")
    assert submit(tmp_path, spec)


def test_the_unfilled_template_is_rejected(tmp_path):
    template = (ROOT / "docs" / "spec-template.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="empty heading: Purpose and cause"):
        submit(tmp_path, template)


def test_source_and_review_only_jobs_skip_the_check(tmp_path):
    assert submit(tmp_path, "no headings at all\n", "source")
    other = tmp_path / "ro"
    other.mkdir()
    data = payload(other)
    (other / "source" / "spec.md").write_text("plain\n", encoding="utf-8")
    data = {k: v for k, v in data.items() if k not in {"outputs", "check_command", "checklist"}}
    data.pop("progress")
    data.update(review="code", review_target={"files": ["spec.md"]})
    assert Jobs(tmp_path / "root2").submit(data, 10).review_target is not None


def test_the_template_file_and_the_check_name_the_same_headings():
    text = (ROOT / "docs" / "spec-template.md").read_text(encoding="utf-8")
    assert tuple(spec_sections(text).keys()) == SPEC_HEADINGS
    assert len(SPEC_HEADINGS) == 13


# --- the design_check ----------------------------------------------------------------------


def with_check(check) -> str:
    return json.dumps(
        {
            "verdict": "PASS",
            "blocking": False,
            "reviewer": "r",
            "checked": "the diff",
            "not_checked": "runtime",
            "design_check": check,
        }
    )


def test_a_full_design_check_is_read():
    assert _design_check(CODE_PASS, []) == DESIGN_CHECK
    assert _verdict_line(CODE_FAIL)[0] == "FAIL"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("portability"),
        lambda c: c.update(extra=c["choices"]),
        lambda c: c["choices"].update(answer="maybe"),
        lambda c: c["choices"].update(evidence=" "),
        lambda c: c["choices"].pop("evidence"),
        lambda c: c.update(choices="ok"),
        lambda c: c["overlaps"].update(evidence=c["one_home"]["evidence"]),
    ],
    ids=[
        "missing key",
        "extra key",
        "answer",
        "empty evidence",
        "no evidence",
        "not object",
        "copy",
    ],
)
def test_a_malformed_design_check_is_rejected(mutate):
    check = {key: dict(value) for key, value in DESIGN_CHECK.items()}
    mutate(check)
    with pytest.raises(ValueError, match="design_check"):
        _design_check(with_check(check), [])


def test_a_missing_design_check_is_rejected():
    with pytest.raises(ValueError, match="design_check"):
        _design_check('{"verdict": "PASS", "blocking": false, "reviewer": "r"}', [])


def test_a_finding_answer_needs_a_matching_finding_line():
    check = {key: dict(value) for key, value in DESIGN_CHECK.items()}
    check["alternatives"] = {"answer": "finding", "evidence": "only one option, spec.md:10"}
    finding = {
        "finding": "F1",
        "severity": "P2",
        "category": "intent",
        "rule": "design/alternatives",
        "location": "spec.md:10",
    }
    body = "\n".join([with_check(check), "### F1", "text", json.dumps(finding)])
    assert _design_check(body, _finding_lines(body, "PASS"))["alternatives"]["answer"] == "finding"
    with pytest.raises(ValueError, match="design/alternatives"):
        _design_check(with_check(check), [])
    other = body.replace("design/alternatives", "design/choices")
    with pytest.raises(ValueError, match="design/alternatives"):
        _design_check(other, _finding_lines(other, "PASS"))


@pytest.mark.parametrize(
    "key, rule, evidence",
    [
        ("alternatives", "design/alternatives", "the omp message has one option, spec.md:12"),
        ("choices", "design/choices", "info/exclude in the shared .git is not listed"),
        ("needed_now", "design/needed_now", "README status wording has no stable-doc owner"),
    ],
    ids=["omp message", "info/exclude", "README status"],
)
def test_the_audit_cases_can_be_expressed(key, rule, evidence):
    """Replay shapes only: the format can say what the three audited reviews missed."""
    check = {k: dict(v) for k, v in DESIGN_CHECK.items()}
    check[key] = {"answer": "finding", "evidence": evidence}
    finding = {
        "finding": "F1",
        "severity": "P2",
        "category": "intent",
        "rule": rule,
        "location": "x",
    }
    body = "\n".join([with_check(check), "### F1", "text", json.dumps(finding)])
    assert _design_check(body, _finding_lines(body, "PASS"))[key]["evidence"] == evidence
    assert set(DESIGN_CHECK) == set(DESIGN_KEYS)


# --- the review step -----------------------------------------------------------------------


def ready(env: Env) -> None:
    env.seed(
        {
            "out.txt": "ok\n",
            "PROGRESS.md": "done\n",
            "checklist.json": '{"items": [{"id": "a", "passes": true}]}',
            "brief.md": BRIEF,
            "choices.json": CHOICES,
        }
    )
    assert env.run("check") == 0


NO_CHECK = (
    '{"verdict": "PASS", "blocking": false, "reviewer": "r",'
    ' "checked": "the diff", "not_checked": "runtime"}'
)


def test_a_design_review_needs_the_design_check(tmp_path, monkeypatch):
    target = tmp_path / "tgt"
    target.mkdir()
    env = Env(
        tmp_path, monkeypatch, review="design", target=target, outputs=["out.txt", "choices.json"]
    )
    ready(env)
    env.scenario([{"reply": NO_CHECK}, {"reply": NO_CHECK}, {"reply": CODE_PASS}])
    assert env.run("review") == 0
    record = env.get().reviews[-1]
    assert not record["ok"] and any("design_check" in p for p in record["problems"])
    assert env.run("review") == 0
    assert env.get().reviews[-1]["ok"] and env.get().reviews[-1]["design_check"] == DESIGN_CHECK


def test_a_source_review_needs_no_design_check(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    ready(env)
    env.scenario([{"reply": NO_CHECK}])
    assert env.run("review") == 0
    assert env.get().reviews[-1]["ok"] and "design_check" not in env.get().reviews[-1]


def test_the_review_message_carries_the_approval_but_not_the_wait_command(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    ready(env)
    env.scenario([{"reply": NO_CHECK}])
    assert env.run("review") == 0
    prompt = env.prompts()[-1]
    assert "Approval source: msg-1" in prompt and "Approval scope: task-1" in prompt
    assert "job_cli" not in prompt and "--reason" not in prompt


@pytest.mark.parametrize("missing", ["checked", "not_checked"])
def test_a_review_must_say_what_it_checked_and_did_not_check(tmp_path, monkeypatch, missing):
    env = Env(tmp_path, monkeypatch)
    ready(env)
    partial = json.loads(NO_CHECK)
    partial[missing] = " "
    env.scenario([{"reply": json.dumps(partial)}] * 2 + [{"reply": NO_CHECK}])
    assert env.run("review") == 0
    record = env.get().reviews[-1]
    assert not record["ok"] and f"the verdict line needs a nonempty {missing}" in record["problems"]
    assert env.get().stage == "review"  # an answer without a verdict reviews again, no fix
    env.put("review")
    assert run_job(env.jobs, env.config, env.job.id, env.attempt) == 0
    record = env.get().reviews[-1]
    assert (record["ok"], record["checked"], record["not_checked"]) == (True, "the diff", "runtime")

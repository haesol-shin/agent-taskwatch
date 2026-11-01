"""One review for code and public text: fingerprints, stale text, the snapshot, `taskwatch text`."""

import os
import shutil
import stat
from pathlib import Path

import pytest

from taskwatch import brief, job_text
from taskwatch.fingerprint import text_fingerprint
from taskwatch.job_cli import main
from taskwatch.job_delivery import _message
from taskwatch.job_env import job_environment
from taskwatch.job_runner import run_job
from tests.git_fixture import git
from tests.test_public_text import UnitEnv
from tests.verdicts import CODE_PASS

REPLY = {"reply": f"{CODE_PASS}\nNo findings."}
PLAIN_PASS = (
    '{"verdict": "PASS", "blocking": false, "reviewer": "r",'
    ' "checked": "the diff", "not_checked": "runtime"}'
)


@pytest.fixture
def unit_env(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch)
    main(["--root", str(tmp_path / "root"), "init"])
    return env


def reviewed(env: UnitEnv, turns: int = 1) -> None:
    """Commit, pass the check and the review."""
    env.scenario([REPLY] * turns)
    env.commit("feat(x): add\n\nWhy it is needed.")
    assert env.problems() == []
    assert env.run("review") == 0
    assert env.get().stage == "deliver_pending"


def text_command(env: UnitEnv, tmp_path: Path, *extra: str) -> int:
    root = str(tmp_path / "root")
    return main(["--root", root, "text", env.job.id, "--out", str(tmp_path / "out"), *extra])


# --- fingerprint ---------------------------------------------------------------------------


def test_text_fingerprint_follows_title_body_and_commit_messages():
    base = text_fingerprint("feat: a", "body", ["feat: a\n\nwhy"])
    assert text_fingerprint("feat: b", "body", ["feat: a\n\nwhy"]) != base
    assert text_fingerprint("feat: a", "body2", ["feat: a\n\nwhy"]) != base
    assert text_fingerprint("feat: a", "body", ["feat: c\n\nwhy"]) != base
    assert text_fingerprint("feat: a", "body", ["feat: a\n\nwhy", "fix: z"]) != base


def test_snapshot_messages_keep_commit_order_past_99_commits(tmp_path):
    messages = [f"feat: change {n}" for n in range(1, 111)]
    unit = job_text.UnitText(
        id="u", base="b", head="h", tree="t", title="t", body="b", messages=tuple(messages)
    )
    for name, text in job_text._files({"u": unit}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    got = job_text.snapshot_messages(tmp_path / "u")
    assert [m.rstrip("\n") for m in got] == messages


def test_the_evidence_line_is_not_part_of_the_fingerprint():
    plain = text_fingerprint("t", "body\n", ["m"])
    line = "Review evidence: round 1 PASS, code abc1234, text 12345678"
    assert text_fingerprint("t", f"body\n\n{line}\n", ["m"]) == plain
    assert text_fingerprint("t", f"body\n\n{line}\n", ["m"]) == text_fingerprint(
        "t", "body\n\nReview evidence: round 9 PASS, code fffffff, text 00000000\n", ["m"]
    )
    # Any other added line still counts.
    assert text_fingerprint("t", "body\n\nExtra\n", ["m"]) != plain


# --- stale text and re-review --------------------------------------------------------------


def test_a_body_edit_after_a_pass_starts_a_text_only_round(unit_env):
    reviewed(unit_env, turns=2)
    reviewed_tree = unit_env.get().reviews[-1]["texts"]["pr"]["tree"]
    unit_env.write_text("body.md", "a changed claim\n")
    assert unit_env.run("review") == 0
    job = unit_env.get()
    assert job.stage == "check" and job.texts == {}  # stale: back to the check
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert "tree is identical to the reviewed tree; only text changed" in prompt
    assert "+a changed claim" in prompt and "-body" in prompt
    assert "See: git diff" not in prompt
    assert unit_env.get().texts["pr"]["tree"] == reviewed_tree


def test_an_amended_message_with_the_same_tree_is_a_text_only_round(unit_env):
    reviewed(unit_env, turns=2)
    old_head = unit_env.get().reviews[-1]["fingerprint"]
    git(unit_env.tree, "commit", "--amend", "-m", "feat(x): add it\n\nWhy it is needed.")
    assert unit_env.run("review") == 0
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert unit_env.get().fingerprint != old_head
    assert "only text changed" in prompt and "+feat(x): add it" in prompt


def test_a_changed_tree_gets_the_code_diff_line(unit_env):
    reviewed(unit_env, turns=2)
    old_head = unit_env.get().reviews[-1]["fingerprint"]
    (unit_env.tree / "more.txt").write_text("more\n", encoding="utf-8")
    git(unit_env.tree, "add", "more.txt")
    git(unit_env.tree, "commit", "-m", "fix(x): more\n\nA reason.")
    assert unit_env.run("review") == 0
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    assert f"See: git diff {old_head}" in prompt and "only text changed" not in prompt


def test_the_evidence_line_alone_does_not_make_the_review_stale(unit_env):
    reviewed(unit_env, turns=2)
    unit_env.write_text("body.md", "body\n\nReview evidence: round 1 PASS, code x, text y\n")
    assert unit_env.run("review") == 0
    assert unit_env.get().stage == "deliver_pending" and len(unit_env.get().reviews) == 2


def test_the_reviewer_message_names_the_snapshot_and_the_unit_range(unit_env):
    unit_env.scenario([REPLY])
    unit_env.commit("feat(x): add\n\nWhy.")
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    prompt = unit_env.prompts()[-1]
    state = unit_env.get().texts["pr"]
    assert "Public text snapshot (read only)" in prompt
    assert f"git diff {state['base']} {state['head']}" in prompt


def test_the_snapshot_is_read_only_and_its_hash_is_recorded(unit_env):
    reviewed(unit_env)
    record = unit_env.get().reviews[-1]
    root = Path(record["snapshot"])
    assert (root / "pr" / "body.md").read_text() == "body\n"
    assert (root / "pr" / "commits" / "01.txt").read_text().startswith("feat(x): add")
    assert stat.S_IMODE((root / "pr" / "body.md").stat().st_mode) == 0o444
    assert not os.access(root / "pr", os.W_OK) or os.geteuid() == 0
    assert record["snapshot_sha256"] == job_text.snapshot_digest(root)


def test_two_units_are_judged_against_their_own_range_and_go_stale_alone(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch)
    env.commit("feat(x): first\n\nWhy one.")
    first = git(env.tree, "rev-parse", "HEAD")
    (env.tree / "two.txt").write_text("2\n", encoding="utf-8")
    git(env.tree, "add", "two.txt")
    git(env.tree, "commit", "-m", "feat(x): second\n\nWhy two.")
    units = (
        {"id": "pr", "base": "main", "head": first},
        {"id": "pr2", "base": first, "head": "HEAD"},
    )
    (env.folder() / "pr" / "pr2").mkdir()
    for name in ("title.txt", "body.md"):
        (env.folder() / "pr" / "pr2" / name).write_text("text\n", encoding="utf-8")
    env.scenario([REPLY])
    assert env.run("check", public_text=units) == 0
    job = env.get()
    assert job.stage == "review" and job.texts["pr"]["head"] == first
    assert job.texts["pr2"]["base"] == first
    assert env.run("review") == 0
    prompt = env.prompts()[-1]
    assert f"git diff {first} {job.texts['pr2']['head']}" in prompt
    env_vars = job_environment({})
    before = job_text.states(job_text.collect(env.get(), env.tree, env.folder(), env_vars))
    (env.folder() / "pr" / "pr2" / "body.md").write_text("edited\n", encoding="utf-8")
    after = job_text.states(job_text.collect(env.get(), env.tree, env.folder(), env_vars))
    assert before["pr"] == after["pr"] and before["pr2"] != after["pr2"]


# --- taskwatch text ------------------------------------------------------------------------


def test_text_exits_3_before_any_pass(unit_env, tmp_path, capsys):
    assert text_command(unit_env, tmp_path) == 3
    assert not (tmp_path / "out").exists()


def test_text_writes_the_reviewed_copy_with_one_evidence_line(unit_env, tmp_path, capsys):
    reviewed(unit_env)
    assert text_command(unit_env, tmp_path) == 0
    out = capsys.readouterr().out.split()
    assert out == [str(tmp_path / "out" / "title.txt"), str(tmp_path / "out" / "body.md")]
    assert (tmp_path / "out" / "title.txt").read_text() == "feat(x): add\n"
    state = unit_env.get().texts["pr"]
    body = (tmp_path / "out" / "body.md").read_text()
    assert body == (
        f"body\n\nReview evidence: round 1 PASS, code {state['head'][:7]}, "
        f"text {state['fingerprint'][:8]}\n"
    )


def test_text_hands_out_the_reviewed_bytes_when_only_the_evidence_line_was_added(
    unit_env, tmp_path
):
    reviewed(unit_env)
    unit_env.write_text("body.md", "body\n\nReview evidence: round 0 PASS, code x, text y\n")
    assert text_command(unit_env, tmp_path) == 0
    assert (tmp_path / "out" / "body.md").read_text().startswith("body\n\nReview evidence: round 1")


def test_text_exits_2_after_a_body_edit(unit_env, tmp_path, capsys):
    reviewed(unit_env)
    unit_env.write_text("body.md", "something else\n")
    assert text_command(unit_env, tmp_path) == 2
    assert "text changed since review" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_text_exits_2_after_a_title_edit(unit_env, tmp_path):
    reviewed(unit_env)
    unit_env.write_text("title.txt", "feat(x): other\n")
    assert text_command(unit_env, tmp_path) == 2


def test_a_hand_edited_snapshot_file_makes_text_exit_2(unit_env, tmp_path, capsys):
    reviewed(unit_env)
    body = Path(unit_env.get().reviews[-1]["snapshot"]) / "pr" / "body.md"
    body.chmod(0o644)
    body.write_text("tampered\n", encoding="utf-8")
    assert text_command(unit_env, tmp_path) == 2
    assert "snapshot changed" in capsys.readouterr().err


def test_a_named_unit_must_exist(unit_env, tmp_path):
    reviewed(unit_env)
    assert text_command(unit_env, tmp_path, "--pr", "pr") == 0
    assert text_command(unit_env, tmp_path, "--pr", "missing") == 1


# --- delivery ------------------------------------------------------------------------------


def finding(ident, severity, rule, location, category="evidence"):
    return {
        "finding": ident,
        "severity": severity,
        "category": category,
        "rule": rule,
        "location": location,
    }


def with_findings(env: UnitEnv, findings: list[dict]):
    record = {**env.get().reviews[-1], "findings": findings}
    env.put("deliver_pending", reviews=(record,))
    return env.get()


def test_the_brief_ends_with_text_units_and_their_fingerprints(unit_env):
    reviewed(unit_env)
    job = with_findings(unit_env, [])
    (unit_env.folder() / "delivery-review.txt").write_text("body", encoding="utf-8")
    state = job.reviews[-1]["texts"]["pr"]
    assert brief.trailer(unit_env.jobs, job)[1:] == [
        f"Review: {unit_env.folder() / 'delivery-review.txt'}",
        f"Public text pr: {unit_env.folder() / 'pr' / 'pr'}",
        f"Text fingerprint pr: {state['fingerprint']}",
    ]
    message = _message(unit_env.jobs, unit_env.config, job).read_text(encoding="utf-8")
    assert "Public text" not in message and "Text fingerprint" not in message


def test_the_brief_lists_each_finding_on_its_own_line(unit_env):
    reviewed(unit_env)
    job = with_findings(
        unit_env,
        [
            finding("F1", "P2", "K-15", "body.md:3"),
            finding("F2", "P1", "K-15", "body.md:9"),
            finding("F3", "P2", "K-10", "README.md:4", "clarity"),
        ],
    )
    lines = brief.compose(unit_env.jobs, unit_env.config, job)
    assert [x for x in lines if x.startswith("- F")] == [
        "- F2 P1 K-15 (evidence): body.md:9",
        "- F1 P2 K-15 (evidence): body.md:3",
        "- F3 P2 K-10 (clarity): README.md:4",
    ]


def test_the_brief_lists_a_p3_finding_and_keeps_other_findings_apart(unit_env):
    reviewed(unit_env)
    job = with_findings(
        unit_env,
        [
            finding("F1", "P3", "K-6", "a.py:1", "clarity"),
            finding("F2", "P2", "other", "b.py:2", "other"),
            finding("F3", "P2", "other", "c.py:3", "other"),
        ],
    )
    message = "\n".join(brief.compose(unit_env.jobs, unit_env.config, job))
    assert "- F1 P3 K-6 (clarity): a.py:1" in message
    assert "- F2 P2 other (other): b.py:2" in message and "- F3 P2 other (other): c.py:3" in message


def test_a_code_review_without_a_design_check_is_not_accepted(unit_env):
    unit_env.scenario([{"reply": PLAIN_PASS}] * 2)
    unit_env.commit("feat(x): add\n\nWhy.")
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    record = unit_env.get().reviews[-1]
    assert not record["ok"] and any("design_check" in p for p in record["problems"])


def test_text_works_after_the_work_folder_was_removed(unit_env, tmp_path):
    reviewed(unit_env)
    shutil.rmtree(unit_env.tree)
    assert text_command(unit_env, tmp_path) == 0
    assert (tmp_path / "out" / "title.txt").read_text() == "feat(x): add\n"
    unit_env.write_text("body.md", "edited later\n")
    assert text_command(unit_env, tmp_path) == 2


def test_no_text_change_means_no_only_text_changed_claim(unit_env):
    reviewed(unit_env, turns=2)
    unit_env.put("review")
    assert run_job(unit_env.jobs, unit_env.config, unit_env.job.id, unit_env.attempt) == 0
    prompt = unit_env.prompts()[-1]
    assert "only text changed" not in prompt and "Text changed since" not in prompt

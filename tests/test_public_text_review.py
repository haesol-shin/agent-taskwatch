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


def test_text_fingerprint_follows_every_reviewed_input():
    inputs = ["feat: a\n", "body", ["feat: a\n\nwhy"], "- Preserve reviewed bytes\n"]
    base = text_fingerprint(*inputs)
    for index, changed in enumerate(
        ["feat: b\n", "body2", ["feat: c\n\nwhy"], "- Reject unreviewed bytes\n"]
    ):
        variant = inputs.copy()
        variant[index] = changed
        assert text_fingerprint(*variant) != base
    assert text_fingerprint(*inputs[:3], inputs[3] + "\n") != base


def test_snapshot_messages_keep_commit_order_past_99_commits(tmp_path):
    messages = [f"feat: change {n}" for n in range(1, 111)]
    unit = job_text.UnitText(
        id="u",
        base="b",
        head="h",
        tree="t",
        title="t",
        body="b",
        commit="- Preserve order\n",
        messages=tuple(messages),
    )
    for name, text in job_text._files({"u": unit}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    got = job_text.snapshot_messages(tmp_path / "u")
    assert [m.rstrip("\n") for m in got] == messages


def test_the_evidence_line_is_not_part_of_the_fingerprint():
    plain = text_fingerprint("t", "body\n", ["m"], "squash")
    line = "Review evidence: round 1 PASS, code abc1234, text 12345678"
    assert text_fingerprint("t", f"body\n\n{line}\n", ["m"], "squash") == plain
    assert text_fingerprint("t", "body\n\nExtra\n", ["m"], "squash") != plain
    assert text_fingerprint("t", "body\n", ["m"], f"squash\n{line}") != plain


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


def advance(repo: Path, ref: str) -> None:
    """Move only `ref` to a new commit in the target repository."""
    new = git(repo, "commit-tree", "main^{tree}", "-p", ref, "-m", "advance")
    git(repo, "branch", "-f", ref, new)


def test_a_moved_external_ref_alone_does_not_make_the_review_stale(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch, base="moving")
    main(["--root", str(tmp_path / "root"), "init"])
    repo = tmp_path / "repo"
    pinned = git(repo, "rev-parse", "moving")
    reviewed(env, turns=2)
    variables = job_environment({})
    before = job_text.states(job_text.collect(env.get(), env.tree, env.folder(), variables))
    advance(repo, "moving")
    assert git(repo, "rev-parse", "moving") != pinned
    after = job_text.states(job_text.collect(env.get(), env.tree, env.folder(), variables))
    assert after == before and before["pr"]["base"] == pinned
    assert env.get().public_text[0]["base"] == pinned
    assert env.run("review") == 0
    job = env.get()
    assert job.stage == "deliver_pending" and job.texts["pr"]["base"] == pinned


def test_a_changed_head_still_invalidates_the_review_after_the_ref_moved(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch, base="moving")
    main(["--root", str(tmp_path / "root"), "init"])
    reviewed(env, turns=2)
    advance(tmp_path / "repo", "moving")
    (env.tree / "more.txt").write_text("more\n", encoding="utf-8")
    git(env.tree, "add", "more.txt")
    git(env.tree, "commit", "-m", "fix(x): more\n\nA reason.")
    assert env.run("review") == 0
    assert env.get().stage == "check" and env.get().texts == {}


def test_a_subset_head_cannot_pass_the_check(unit_env):
    unit_env.commit("feat(x): first\n\nWhy one.")
    first = git(unit_env.tree, "rev-parse", "HEAD")
    git(unit_env.tree, "commit", "--allow-empty", "-m", "fix(x): second\n\nWhy two.")
    unit = {**unit_env.get().public_text[0], "head": first}
    assert unit_env.run("check", public_text=(unit,)) == 0
    job = unit_env.get()
    assert job.stage == "fix"
    assert any("complete job base and result HEAD" in p for p in job.checks[-1]["problems"])
    assert job.texts == {}


def test_the_snapshot_is_read_only_and_its_hash_is_recorded(unit_env):
    reviewed(unit_env)
    record = unit_env.get().reviews[-1]
    root = Path(record["snapshot"])
    assert (root / "pr" / "body.md").read_text() == "body\n"
    assert (root / "pr" / "commits" / "01.txt").read_text().startswith("feat(x): add")
    assert (root / "pr" / "commit.txt").read_text() == "- Preserve the complete result\n"
    assert record["texts"]["pr"]["text_contract"] == 2
    assert stat.S_IMODE((root / "pr" / "body.md").stat().st_mode) == 0o444
    assert not os.access(root / "pr", os.W_OK) or os.geteuid() == 0
    assert record["snapshot_sha256"] == job_text.snapshot_digest(root)


def test_a_commit_body_edit_invalidates_review_and_copy(unit_env, tmp_path):
    reviewed(unit_env, turns=2)
    unit_env.write_text("commit.txt", "- Reject unreviewed publication\n")
    assert text_command(unit_env, tmp_path) == 2
    assert not (tmp_path / "out").exists()
    assert unit_env.run("review") == 0
    assert unit_env.get().stage == "check"
    assert unit_env.problems() == []
    assert unit_env.run("review") == 0
    assert text_command(unit_env, tmp_path) == 0
    assert (tmp_path / "out" / "commit.txt").read_text() == "- Reject unreviewed publication\n"


def test_historical_multi_unit_evidence_stays_readable_without_promotion(
    unit_env, tmp_path, capsys
):
    reviewed(unit_env)
    record = dict(unit_env.get().reviews[-1])
    snapshot = Path(record["snapshot"])
    (snapshot / "pr").chmod(0o755)
    (snapshot / "pr" / "commit.txt").unlink()
    state = dict(record["texts"]["pr"])
    state.pop("text_contract")
    state["fingerprint"] = text_fingerprint(
        "feat(x): add\n", "body\n", job_text.snapshot_messages(snapshot / "pr"), None
    )
    record.update(texts={"pr": state}, snapshot_sha256=job_text.snapshot_digest(snapshot))
    units = (*unit_env.get().public_text, {"id": "old", "base": state["head"], "head": "HEAD"})
    unit_env.put("deliver_pending", public_text=units, texts={"pr": state}, reviews=(record,))
    loaded = unit_env.jobs.load()[unit_env.job.id]
    assert len(loaded.public_text) == 2
    # Historical fingerprints and snapshots used universal-newline reads.
    for name, value in (("title.txt", b"feat(x): add\r\n"), ("body.md", b"body\r\n")):
        (unit_env.folder() / "pr/pr" / name).write_bytes(value)
    unit_env.write_text("commit.txt", "- This live file was never reviewed\n")
    assert text_command(unit_env, tmp_path, "--pr", "pr") == 0
    assert "Historical text:" in capsys.readouterr().out
    assert not (tmp_path / "out" / "commit.txt").exists()
    with pytest.raises(ValueError, match="historical"):
        job_text.collect(loaded, unit_env.tree, unit_env.folder(), job_environment({}))


def test_historical_copy_refuses_a_stale_squash_file(unit_env, tmp_path):
    reviewed(unit_env)
    record = dict(unit_env.get().reviews[-1])
    state = dict(record["texts"]["pr"])
    state.pop("text_contract")
    state["fingerprint"] = text_fingerprint(
        "feat(x): add\n",
        "body\n",
        job_text.snapshot_messages(Path(record["snapshot"]) / "pr"),
        None,
    )
    record["texts"] = {"pr": state}
    unit_env.put("deliver_pending", texts={"pr": state}, reviews=(record,))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "commit.txt").write_text("stale\n")
    assert text_command(unit_env, tmp_path) == 1
    assert not (tmp_path / "out" / "title.txt").exists()


# --- taskwatch text ------------------------------------------------------------------------


def test_text_exits_3_before_any_pass(unit_env, tmp_path, capsys):
    assert text_command(unit_env, tmp_path) == 3
    assert not (tmp_path / "out").exists()


def test_text_writes_the_reviewed_copy_with_one_evidence_line(unit_env, tmp_path, capsys):
    reviewed(unit_env)
    assert text_command(unit_env, tmp_path) == 0
    out = capsys.readouterr().out.split()
    assert out == [str(tmp_path / "out" / name) for name in ("title.txt", "body.md", "commit.txt")]
    assert (tmp_path / "out" / "commit.txt").read_text() == "- Preserve the complete result\n"
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


def test_a_partial_base_cannot_pass_the_check(unit_env):
    unit_env.commit("feat(x): first\n\nWhy one.")
    first = git(unit_env.tree, "rev-parse", "HEAD")
    git(unit_env.tree, "commit", "--allow-empty", "-m", "fix(x): second\n\nWhy two.")
    unit = {**unit_env.get().public_text[0], "base": first}
    assert unit_env.run("check", public_text=(unit,)) == 0
    job = unit_env.get()
    assert job.stage == "fix" and job.texts == {}
    assert any("complete job base and result HEAD" in p for p in job.checks[-1]["problems"])


def test_a_blank_pr_body_cannot_be_reviewed(unit_env):
    unit_env.commit("feat(x): add\n\nWhy.")
    unit_env.write_text("body.md", " \n")
    assert any("nonblank" in p for p in unit_env.problems())
    assert unit_env.get().stage == "fix"

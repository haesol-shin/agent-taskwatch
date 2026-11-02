"""Round 2 and later of a code review: the delta, and the conditions of a full review."""

import os
from pathlib import Path

import pytest

from taskwatch import review_delta
from taskwatch.job_cli import main
from tests.git_fixture import git, make_repo
from tests.test_public_text import UnitEnv
from tests.verdicts import CODE_PASS

REPLY = {"reply": f"{CODE_PASS}\nNo findings."}


def commit(repo: Path, files: dict[str, str], message: str = "work") -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(repo, "add", name)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def lines(n: int, tag: str = "a") -> str:
    return "".join(f"{tag}{i}\n" for i in range(n))


def review(fingerprint: str, base: str, *findings: dict) -> dict:
    return {"fingerprint": fingerprint, "base": base, "findings": list(findings)}


def finding(rule: str = "K-4", severity: str = "P1", location: str = "a.py:1") -> dict:
    return {"finding": "F1", "severity": severity, "rule": rule, "location": location}


class Round:
    """A repo with a first reviewed commit of 20 lines and a fix on top."""

    def __init__(self, tmp_path: Path, name: str = "repo") -> None:
        self.repo = make_repo(tmp_path, name)
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.first = commit(self.repo, {"a.py": lines(20)}, "feat: first")

    def decide(self, fix: dict[str, str], *, scope: str = "", prior=None, checks=(), base=None):
        head = commit(self.repo, fix, "fix: second")
        return review_delta.decide(
            self.repo,
            os.environ,
            base=self.base if base is None else base,
            fingerprint=head,
            approval_scope=scope,
            prior=prior or [review(self.first, self.base)],
            checks=list(checks),
        )


@pytest.fixture
def rnd(tmp_path):
    return Round(tmp_path)


def other(rnd: Round, name: str) -> Round:
    """A second repo for a second case in the same test: a delta runs from the first commit."""
    return Round(rnd.repo.parent, name)


def test_a_small_fix_in_a_reviewed_file_is_a_delta_round(rnd):
    scope = rnd.decide({"a.py": lines(20).replace("a3", "fixed")})
    assert (scope.mode, scope.reasons, scope.new_files) == ("delta", (), ())
    assert (scope.lines, scope.first_lines, scope.ratio) == (2, 20, 0.1)
    assert scope.files == ("a.py",) and "a.py" in scope.stat


def test_f1_a_file_no_earlier_round_saw_is_read_in_full_and_the_round_stays_a_delta(rnd):
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x"), "b.py": "one\n"})
    assert scope.mode == "delta" and scope.new_files == ("b.py",)
    assert scope.reasons[0].startswith("F1:") and "b.py" in scope.reasons[0]
    text = "\n".join(review_delta.message_lines(scope, 2, []))
    assert "Read each in full" in text and "b.py" in text and "full review" not in text


def test_f1_does_not_fire_again_for_a_file_a_later_round_already_read(rnd):
    second = commit(rnd.repo, {"b.py": "one\n"}, "fix: add b")
    prior = [review(rnd.first, rnd.base), review(second, rnd.base)]
    scope = rnd.decide({"b.py": "two\n"}, prior=prior)
    assert scope.mode == "delta" and scope.new_files == ()


def test_f2_a_delta_over_half_of_the_first_diff_is_a_full_review(rnd):
    scope = rnd.decide({"a.py": lines(20, "z")})  # 40 changed lines against 20
    assert scope.mode == "full" and scope.reasons[0].startswith("F2:") and scope.ratio == 2.0
    text = "\n".join(review_delta.message_lines(scope, 2, []))
    assert "a full review" in text and "F2:" in text


def test_f2_a_small_delta_stays_a_delta_and_a_rewrite_does_not(rnd):
    scope = rnd.decide({"a.py": lines(20).replace("a1\n", "m\n").replace("a2\n", "n\n")})
    assert (scope.lines, scope.ratio, scope.mode) == (4, 0.2, "delta")
    exact = other(rnd, "exact").decide({"a.py": lines(20, "k")[: -len("k19\n")] + "a19\n"})
    assert exact.lines > 10  # all but one line changed: well over half
    assert exact.mode == "full"


def test_f2_400_lines_is_a_full_review_even_when_it_is_under_half(tmp_path):
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, {"big.py": lines(2000)}, "feat: big")
    head = commit(repo, {"big.py": lines(2000).replace("a", "b", 201)}, "fix: many")
    scope = review_delta.decide(
        repo,
        os.environ,
        base=base,
        fingerprint=head,
        approval_scope="",
        prior=[review(first, base)],
        checks=[],
    )
    assert scope.lines > 400 and scope.ratio is not None and scope.ratio < 0.5
    assert scope.mode == "full" and scope.reasons[0].startswith("F2:")


def test_f3_a_base_that_differs_from_the_last_reviews_is_a_full_review(rnd):
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x")}, base="b" * 40)
    assert scope.mode == "full" and scope.reasons[0].startswith("F3:")
    old = rnd.decide(
        {"a.py": lines(20).replace("a4", "x")}, prior=[{**review(rnd.first, ""), "base": None}]
    )
    assert old.mode == "full"  # a record from before the field exists counts as different


def test_f5_a_p0_in_any_earlier_round_is_a_full_review(rnd):
    prior = [review(rnd.first, rnd.base, finding(severity="P0"))]
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x")}, prior=prior)
    assert scope.mode == "full" and any(r.startswith("F5:") for r in scope.reasons)
    calm = other(rnd, "calm")
    prior = [review(calm.first, calm.base, finding(severity="P1"))]
    assert calm.decide({"a.py": lines(20).replace("a5", "x")}, prior=prior).mode == "delta"


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", "deploy/.env.prod", "keys/server.pem", "app/auth/login.py"]
)
def test_f6_a_security_path_in_the_delta_is_a_full_review(rnd, path):
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x"), path: "x\n"})
    assert scope.mode == "full" and any(r.startswith("F6:") and path in r for r in scope.reasons)


def test_f6_a_path_the_approval_scope_names_is_a_full_review(rnd):
    fix = {"a.py": lines(20).replace("a3", "x"), "docs/guide/x.md": "x\n"}
    assert rnd.decide(fix, scope="Write only in docs/guide/").mode == "full"
    elsewhere = {"a.py": lines(20).replace("a4", "x"), "docs/other/x.md": "x\n"}
    assert other(rnd, "elsewhere").decide(elsewhere, scope="Write only in docs/guide/").mode == (
        "delta"
    )


def test_f7_a_rule_that_came_back_is_a_full_review(rnd):
    again = [
        review(rnd.first, rnd.base, finding("K-7", location="a.py:1")),
        review(rnd.first, rnd.base, finding("K-7", location="a.py:9")),
    ]
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x")}, prior=again)
    assert scope.mode == "full" and any(r.startswith("F7:") and "K-7" in r for r in scope.reasons)
    noted = other(rnd, "noted").decide(
        {"a.py": lines(20).replace("a5", "x")}, checks=[{"recurred": {"K-9": "p"}}]
    )
    assert noted.mode == "full" and "K-9" in noted.reasons[-1]


def test_f7_the_catch_all_rule_other_is_not_a_repeat(rnd):
    again = [
        review(rnd.first, rnd.base, finding("other")),
        review(rnd.first, rnd.base, finding("other")),
    ]
    assert rnd.decide({"a.py": lines(20).replace("a3", "x")}, prior=again).mode == "delta"


def test_a_diff_that_cannot_be_read_is_a_full_review(rnd):
    prior = [review("0" * 40, rnd.base)]
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x")}, prior=prior)
    assert scope.mode == "full" and scope.lines is None and "cannot be read" in scope.reasons[-1]
    assert "Delta since" not in "\n".join(review_delta.message_lines(scope, 2, []))


def test_new_findings_counts_rule_and_location_pairs_no_round_reported():
    prior = [review("x", "b", finding("K-1", location="a:1"), finding("K-2", location="b:2"))]
    now = [finding("K-1", location="a:1"), finding("K-1", location="a:5"), finding("K-3")]
    assert review_delta.new_findings(prior, now) == 2
    assert review_delta.new_findings([], now) == 3


# --- through the runner ------------------------------------------------------------------


@pytest.fixture
def unit_env(tmp_path, monkeypatch):
    env = UnitEnv(tmp_path, monkeypatch)
    main(["--root", str(tmp_path / "root"), "init"])
    return env


def first_round(env: UnitEnv) -> None:
    env.scenario([REPLY, REPLY])
    commit(env.tree, {"big.txt": lines(20), "out.txt": "ok\n"}, "feat(x): add\n\nWhy.")
    assert env.problems() == []
    assert env.run("review") == 0


def second_round(env: UnitEnv, files: dict[str, str]) -> str:
    commit(env.tree, files, "fix(x): more\n\nA reason.")
    assert env.run("review") == 0  # the result changed: back to the check
    assert env.problems() == []
    assert env.run("review") == 0
    return env.prompts()[-1]


def test_the_second_round_message_holds_the_delta_the_check_and_the_ratio(unit_env):
    first_round(unit_env)
    old = unit_env.get().reviews[-1]["fingerprint"]
    prompt = second_round(unit_env, {"big.txt": lines(20).replace("a3", "fixed")})
    assert "Review the fix, not the whole job again" in prompt
    assert f"Previously reviewed fingerprint: {old}" in prompt
    assert (
        "Delta since the last review: 2 lines in 1 files (9% of the first review's diff)" in prompt
    )
    assert "Files the delta touches: big.txt" in prompt
    assert "git diff --stat of the delta:" in prompt and "big.txt | 2 +-" in prompt
    assert "Last check: passed, exit code 0, log " in prompt
    assert "Findings of the last review:" in prompt and "No findings." in prompt
    assert "a full review" not in prompt


def test_the_round_record_keeps_minutes_new_findings_and_the_ratio(unit_env):
    first_round(unit_env)
    second_round(unit_env, {"big.txt": lines(20).replace("a3", "fixed")})
    one, two = unit_env.get().reviews
    base = unit_env.get().base
    assert (one["base"], one["delta_ratio"], "rereview" not in one) == (base, None, True)
    assert (two["base"], two["delta_ratio"], two["new_findings"]) == (base, 0.09, 0)
    assert isinstance(two["minutes"], float) and two["minutes"] >= 0
    assert two["rereview"]["mode"] == "delta" and list(two["rereview"]["files"]) == ["big.txt"]
    assert (two["rereview"]["lines"], two["rereview"]["first_lines"]) == (2, 22)


def test_a_security_path_in_the_delta_sends_a_full_review(unit_env):
    first_round(unit_env)
    prompt = second_round(unit_env, {".github/workflows/ci.yml": "on: push\n"})
    assert "and a full review" in prompt and "F6:" in prompt
    assert "Review the fix, not the whole job again" not in prompt
    assert unit_env.get().reviews[-1]["rereview"]["mode"] == "full"


def test_a_rebase_onto_newer_work_changes_the_effective_base_and_is_f3(tmp_path):
    repo = make_repo(tmp_path, "rebased")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "work")
    first = commit(repo, {"a.py": lines(20)}, "feat: first")
    assert review_delta.effective_base(repo, os.environ, first, "work", base) == base
    git(repo, "checkout", "-q", "main")
    upstream = commit(repo, {"other.py": "x\n"}, "feat: upstream")
    git(repo, "checkout", "-q", "work")
    git(repo, "rebase", "-q", "main")
    head = commit(repo, {"a.py": lines(20).replace("a3", "x")}, "fix: small")
    now = review_delta.effective_base(repo, os.environ, head, "work", base)
    assert now == upstream
    scope = review_delta.decide(
        repo,
        os.environ,
        base=now,
        fingerprint=head,
        approval_scope="",
        prior=[review(first, base)],
        checks=[],
    )
    assert scope.mode == "full" and any(r.startswith("F3:") for r in scope.reasons)


def test_a_branch_without_its_own_commit_keeps_the_recorded_base(rnd):
    head = git(rnd.repo, "rev-parse", "HEAD")
    assert review_delta.effective_base(rnd.repo, os.environ, head, "", rnd.base) == rnd.base


@pytest.mark.parametrize("path", ["credentials.json", "config/secret_key.txt", "secrets.yml"])
def test_f6_a_secret_or_credential_prefix_is_a_security_path(rnd, path):
    scope = rnd.decide({"a.py": lines(20).replace("a3", "x"), path: "x\n"})
    assert scope.mode == "full" and any(r.startswith("F6:") for r in scope.reasons)


def test_f6_an_absolute_path_in_the_approval_scope_is_read_under_the_work_folder(rnd):
    fix = {"a.py": lines(20).replace("a3", "x")}
    text = f"Write only {rnd.repo}/a.py and nothing else"
    scope = review_delta.decide(
        rnd.repo,
        os.environ,
        base=rnd.base,
        fingerprint=commit(rnd.repo, fix, "fix: x"),
        approval_scope=text,
        prior=[review(rnd.first, rnd.base)],
        checks=[],
        roots=(str(rnd.repo),),
    )
    assert scope.mode == "full" and any(r.startswith("F6:") for r in scope.reasons)


@pytest.mark.parametrize("named", ["{root}/src", "{root}/src/", "{root}", "{root}/"])
@pytest.mark.parametrize("where", ["work", "target"])
def test_f6_a_directory_or_the_root_in_the_approval_scope_is_full(tmp_path, named, where):
    rnd = Round(tmp_path)
    other_root = tmp_path / "elsewhere"
    root = rnd.repo if where == "work" else other_root
    roots = (str(rnd.repo), str(other_root))
    scope_text = "Write only in " + named.format(root=root)
    fix = {"src/a.py": lines(20).replace("a3", "x")}
    head = commit(rnd.repo, fix, "fix: x")
    scope = review_delta.decide(
        rnd.repo,
        os.environ,
        base=rnd.base,
        fingerprint=head,
        approval_scope=scope_text,
        prior=[review(rnd.first, rnd.base)],
        checks=[],
        roots=roots,
    )
    assert scope.mode == "full" and any(r.startswith("F6:") for r in scope.reasons)


def test_f6_a_bare_word_outside_a_root_is_not_a_path(rnd):
    fix = {"src/a.py": lines(20).replace("a3", "x")}
    head = commit(rnd.repo, fix, "fix: x")
    scope = review_delta.decide(
        rnd.repo,
        os.environ,
        base=rnd.base,
        fingerprint=head,
        approval_scope="Edit src only if needed",
        prior=[review(rnd.first, rnd.base)],
        checks=[],
        roots=(str(rnd.repo),),
    )
    assert not any(r.startswith("F6:") for r in scope.reasons)

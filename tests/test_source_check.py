import hashlib
import json
from pathlib import Path

import pytest

from taskwatch import source_check
from taskwatch.review_docs import profile_names

TEXT = (
    "The tool released version 2.1 on 2026-03-04 and added “smart quotes” handling.\n"
    "It supports   about 1,234 projects\tin the registry today, according to the maintainers."
)
QUOTE = "added “smart quotes” handling. It supports about 1,234 projects in the registry today"
RESULT = "The registry lists many projects [C1]. A total of 1234 is cited [C2].\n"


def write(work: Path, claims: list[dict], index: dict | None = None, result: str = RESULT) -> None:
    (work / "sources").mkdir(exist_ok=True)
    (work / "sources" / "S1.txt").write_text(TEXT, encoding="utf-8")
    digest = hashlib.sha256(TEXT.encode()).hexdigest()
    entry = {"url": "https://example.invalid/a", "retrieved": "2026-11-01", "kind": "full"}
    index = {"S1": {**entry, "sha256": digest}} if index is None else index
    (work / "sources" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    (work / "claims.json").write_text(json.dumps(claims), encoding="utf-8")
    (work / "result.md").write_text(result, encoding="utf-8")


GOOD = [
    {"id": "C1", "claim": "It has projects", "type": "fact", "source": "S1", "quote": QUOTE},
    {
        "id": "C2",
        "claim": "About 1234 projects are listed",
        "type": "number",
        "source": "S1",
        "quote": "about 1,234 projects",
    },
]


def run(work: Path):
    return source_check.check(work, ("result.md", "claims.json"))


@pytest.fixture
def work(tmp_path: Path) -> Path:
    write(tmp_path, GOOD)
    return tmp_path


@pytest.mark.parametrize(
    ("quote", "text", "found"),
    [
        ("added “smart quotes”", 'added "smart quotes"', True),
        ("it\u2019s", "it's", True),
        ("a \u2013 b \u2014 c \u2212 d", "a - b - c - d", True),
        ("about 1,234  projects\nin", "about 1,234 projects in", True),
        ("about\u00a01,234", "about 1,234", True),
        ("About 1,234", "about 1,234", False),
        ("about 1234", "about 1,234", False),
    ],
)
def test_quotes_match_after_normalizing_whitespace_quote_marks_and_dashes(
    quote: str, text: str, found: bool
) -> None:
    assert (source_check.normalize(quote) in source_check.normalize(text)) is found


def test_numbers_ignore_thousands_separators() -> None:
    assert source_check.numbers("1,234 users, 12.5% and 7") == {"1234", "12.5", "7"}


def test_a_complete_research_result_passes(work: Path) -> None:
    result = run(work)
    assert result.problems == []
    assert (result.claims, result.snippet_only) == (2, 0)
    assert [row[4] for row in result.rows] == [True, True]


def edit(work: Path, change) -> list[str]:
    claims = json.loads((work / "claims.json").read_text(encoding="utf-8"))
    change(claims)
    (work / "claims.json").write_text(json.dumps(claims), encoding="utf-8")
    return run(work).problems


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c[0].update(quote="nothing like what the source says, ever, truly"), "not in"),
        (lambda c: c[0].update(quote="added smart quotes"), "shorter than 8 words"),
        (lambda c: c[1].update(claim="About 9999 projects"), "number of the claim"),
        (lambda c: c[0].update(source="S9"), "needs a quote and a source"),
        (lambda c: c[0].update(type="guess"), "needs a claim text and a type"),
        (lambda c: c[1].update(id="C1"), "used twice"),
    ],
)
def test_a_bad_claim_fails(work: Path, change, message: str) -> None:
    problems = edit(work, change)
    assert any(message in p for p in problems), problems


def test_a_short_number_quote_is_allowed_when_it_holds_the_number(work: Path) -> None:
    assert run(work).problems == []  # C2 quotes "about 1,234 projects": 3 words


def test_absence_needs_the_search_terms_and_derived_needs_its_parents(work: Path) -> None:
    extra = [
        {"id": "C3", "claim": "No tool does X", "type": "absence", "searched": []},
        {"id": "C4", "claim": "Sum is 2468", "type": "derived", "from": ["C2", "C9"]},
    ]
    problems = edit(work, lambda c: c.extend(extra))
    assert any("C3 says nothing was found but lists no searched terms" in p for p in problems)
    assert any("C4 derives from C9" in p for p in problems)
    (work / "result.md").write_text(RESULT + "[C3] [C4]", encoding="utf-8")
    repaired = [{**extra[0], "searched": ["x", "y"]}, {**extra[1], "from": ["C1", "C2"]}]
    fixed = edit(work, lambda c: c.__setitem__(slice(2, 4), repaired))
    assert fixed == []


def test_every_citation_has_a_claim_and_every_claim_a_citation(work: Path) -> None:
    (work / "result.md").write_text("Only [C1] and [C7] here.", encoding="utf-8")
    problems = run(work).problems
    assert "the result cites [C7], which is not in claims.json" in problems
    assert "claim C2 is cited nowhere in the result" in problems


def test_a_changed_or_missing_source_file_fails(work: Path) -> None:
    (work / "sources" / "S1.txt").write_text(TEXT + " edited", encoding="utf-8")
    assert any("does not match its sha256" in p for p in run(work).problems)
    (work / "sources" / "S1.txt").unlink()
    assert any("has no file sources/S1.txt" in p for p in run(work).problems)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"url": ""}, "empty url"),
        ({"retrieved": ""}, "empty retrieved"),
        ({"retrieved": "yesterday"}, "not an ISO date"),
        ({"kind": "page"}, "kind must be full or snippet"),
    ],
)
def test_the_index_needs_url_date_and_kind(work: Path, entry: dict, message: str) -> None:
    index = json.loads((work / "sources" / "index.json").read_text(encoding="utf-8"))
    index["S1"].update(entry)
    (work / "sources" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    assert any(message in p for p in run(work).problems)


def test_claims_that_rest_on_snippets_are_counted_not_refused(work: Path) -> None:
    index = json.loads((work / "sources" / "index.json").read_text(encoding="utf-8"))
    index["S1"]["kind"] = "snippet"
    (work / "sources" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    result = run(work)
    assert result.problems == [] and result.snippet_only == 2


def test_missing_and_malformed_files_are_one_problem_each(tmp_path: Path) -> None:
    assert run(tmp_path).problems == ["required file is missing: claims.json"]
    (tmp_path / "claims.json").write_text("{", encoding="utf-8")
    assert run(tmp_path).problems[0].startswith("claims.json is not valid JSON")


def test_the_table_lists_each_pair_with_its_verdict(work: Path) -> None:
    edit(work, lambda c: c[0].update(quote="a made up quote that the source never says at all"))
    table = source_check.table(run(work).rows).splitlines()
    assert table[2].endswith("| S1 | no |") and table[3].endswith("| S1 | yes |")


def test_the_claims_file_adds_the_source_rules_to_any_review() -> None:
    assert profile_names("design", ("design.md", "claims.json"))[-1] == "source.md"
    assert profile_names("source", ("claims.json",)) == profile_names("source")
    assert "source.md" not in profile_names("design", ("design.md",))


# --- in the runner -------------------------------------------------------------------------


def test_the_check_step_runs_the_source_check_and_hands_the_reviewer_a_table(
    tmp_path, monkeypatch
) -> None:
    from tests.test_job_runner import GOOD_FILES, PASS, Env

    env = Env(tmp_path, monkeypatch, outputs=["out.txt", "result.md", "claims.json"])
    env.seed({**GOOD_FILES, "result.md": RESULT})
    work = env.folder() / "work"
    write(work, GOOD, result=RESULT)
    index = json.loads((work / "sources" / "index.json").read_text(encoding="utf-8"))
    index["S1"]["kind"] = "snippet"
    (work / "sources" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    assert env.run("check") == 0
    check = env.get().checks[-1]
    assert (check["ok"], check["problems"]) == (True, ())
    assert dict(check["sources"]) == {"claims": 2, "snippet_only": 2}
    assert "| C1 |" in (env.folder() / "source-table.md").read_text(encoding="utf-8")
    env.scenario([{"reply": PASS}])
    assert env.run("review") == 0
    prompt = env.prompts()[-1]
    assert f"Source table: {env.folder() / 'source-table.md'}" in prompt


def test_a_quote_missing_from_its_source_sends_the_job_back_to_fix(tmp_path, monkeypatch) -> None:
    from tests.test_job_runner import GOOD_FILES, Env

    env = Env(tmp_path, monkeypatch, outputs=["out.txt", "result.md", "claims.json"])
    env.seed({**GOOD_FILES, "result.md": RESULT})
    work = env.folder() / "work"
    bad = [
        {**GOOD[0], "quote": "a sentence that the saved source does not contain anywhere"},
        GOOD[1],
    ]
    write(work, bad, result=RESULT)
    assert env.run("check") == 0
    assert env.get().stage == "fix"
    assert any("the quote is not in source S1" in p for p in env.get().checks[-1]["problems"])


def test_an_absence_claim_with_a_quote_gets_the_quote_and_number_checks(work: Path) -> None:
    absence = {
        "id": "C3",
        "claim": "Nothing exists after 12 searches",
        "type": "absence",
        "searched": ["a", "b"],
        "source": "S1",
        "quote": "a sentence that the saved source does not contain at all",
    }
    problems = edit(work, lambda c: c.append(absence))
    assert any("C3: the quote is not in source S1" in p for p in problems)
    assert any("C3: a number of the claim is not in its quote" in p for p in problems)
    (work / "result.md").write_text(RESULT + "[C3]", encoding="utf-8")
    ok = {**absence, "claim": "Nothing exists", "quote": "about 1,234 projects"}
    assert edit(work, lambda c: c.__setitem__(2, ok)) == []

"""The job type derives from `review` and `review_target`; contracts load from docs/jobs."""

import tomllib
from pathlib import Path

import pytest

from taskwatch import job_runner
from taskwatch.brief import card, compose
from taskwatch.job_types import job_type_label
from taskwatch.jobs import JobConfig
from taskwatch.review_docs import docs_dir
from tests.test_needs import Ledger, needs

ROOT = Path(__file__).resolve().parent.parent
TARGET = {"files": ("result.txt",)}
CASES = [
    ("source", None, "research"),
    ("design", None, "design"),
    ("code", None, "implement"),
    ("code", TARGET, "review"),
    ("source", TARGET, "review"),
    ("design", TARGET, "review"),
]


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path)


@pytest.mark.parametrize(("review", "target", "label"), CASES)
def test_every_consumer_reads_the_derived_label(ledger, review, target, label):
    ident = ledger.add(stage="user_wait", wait_reason="review_exhausted")
    ledger.set(ident, review=review, review_target=target)
    job = ledger.get(ident)
    assert job_type_label(job) == label
    assert f"job {ident} {label} user_wait review_exhausted" in ledger.list()[1]
    assert compose(ledger.jobs, JobConfig(), job)[0].startswith(f"Job {ident} ({label}):")
    assert card(ledger.jobs, JobConfig(), job)[0] == f"{label} job {ident}: verdict none."


@pytest.mark.parametrize(("review", "label"), [("source", "research"), ("design", "design")])
def test_a_successor_names_the_job_type_of_its_predecessor(ledger, review, label):
    first = ledger.add(stage="done")
    ledger.set(first, review=review)
    gate = ledger.add(
        stage="approval_wait", wait_reason="approval_required", submit={"predecessors": [first]}
    )
    assert f"job {gate} research  after job {first} ({label}) done" in needs(ledger.root)[1]


@pytest.mark.parametrize(
    ("review", "label"), [("source", "research"), ("design", "design"), ("code", "implement")]
)
def test_the_workflow_carries_the_brief_and_the_job_type_contract(ledger, review, label):
    ident = ledger.add(stage="work")
    ledger.set(ident, review=review)
    text = job_runner._job_type_text(ledger.get(ident))
    jobs = docs_dir("jobs")
    brief, own = (jobs / f"{name}.md" for name in ("brief", label))
    assert (
        text
        == brief.read_text(encoding="utf-8").strip()
        + "\n\n"
        + own.read_text(encoding="utf-8").strip()
    )


def test_the_installed_resource_home_holds_every_job_contract():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["data-files"]["share/agent-taskwatch/jobs"]
    packaged = {path.name for pattern in patterns for path in ROOT.glob(pattern)}
    assert packaged == {path.name for path in (ROOT / "docs" / "jobs").iterdir()}
    assert {"brief.md", "design.md", "implement.md", "research.md", "defaults.schema.json"} <= (
        packaged
    )

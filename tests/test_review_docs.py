"""The job copy keeps the installed shape and holds the documents the guides link."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from taskwatch import review_docs

ROOT = Path(__file__).resolve().parents[1]
NAMES = review_docs.profile_names("code")


def test_the_job_copy_resolves_the_guide_links(tmp_path: Path):
    target = tmp_path / "review-docs"
    copied = review_docs.copy_docs(target, NAMES)
    guides = review_docs.guide_dir(target)
    assert "(../checks.md" in (guides / "README.md").read_text(encoding="utf-8")
    assert target / "agent-taskwatch" / "checks.md" in copied
    assert target / "examples" / "omp" / "reviewer.md" in copied
    assert set(guides / name for name in NAMES) <= set(copied)
    assert review_docs.link_problems(target) == []


def test_a_missing_linked_document_is_reported(tmp_path: Path):
    target = tmp_path / "review-docs"
    review_docs.copy_docs(target, NAMES)
    (target / "agent-taskwatch" / "checks.md").unlink()
    assert "agent-taskwatch/review/README.md: link does not resolve: ../checks.md" in (
        review_docs.link_problems(target)
    )


def test_a_link_that_leaves_the_job_copy_is_reported(tmp_path: Path):
    target = tmp_path / "review-docs"
    review_docs.copy_docs(target, NAMES)
    guide = review_docs.guide_dir(target) / "code.md"
    guide.write_text(
        guide.read_text(encoding="utf-8") + "\n[out](../../../outside.md)\n", encoding="utf-8"
    )
    assert "agent-taskwatch/review/code.md: link leaves the tree: ../../../outside.md" in (
        review_docs.link_problems(target)
    )


def test_an_older_flat_guide_keeps_its_text(tmp_path: Path, monkeypatch):
    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "docs", tree / "docs")
    shutil.copytree(ROOT / "examples", tree / "examples")
    (tree / "docs" / "review" / "common.md").write_text("installed\n", encoding="utf-8")
    monkeypatch.setattr(review_docs, "docs_root", lambda: tree / "docs")
    target = tmp_path / "review-docs"
    target.mkdir()
    (target / "common.md").write_text("kept\n", encoding="utf-8")
    review_docs.copy_docs(target, NAMES)
    assert (target / "agent-taskwatch" / "review" / "common.md").read_text(
        encoding="utf-8"
    ) == "kept\n"


def test_a_job_keeps_the_copy_it_started_with(tmp_path: Path):
    target = tmp_path / "review-docs"
    common = review_docs.guide_dir(target) / "common.md"
    review_docs.copy_docs(target, NAMES)
    common.write_text("kept\n", encoding="utf-8")
    review_docs.copy_docs(target, NAMES)
    assert common.read_text(encoding="utf-8") == "kept\n"


def test_a_missing_source_document_is_named(tmp_path: Path, monkeypatch):
    tree = tmp_path / "docs"
    (tree / "review").mkdir(parents=True)
    for name in review_docs.all_names():
        (tree / "review" / name).write_text("# Guide\n", encoding="utf-8")
    (tree / "review" / "README.md").write_text("# Guide\n\n[checks](../checks.md)\n")
    monkeypatch.setattr(review_docs, "docs_root", lambda: tree)
    with pytest.raises(review_docs.DocsError, match="checks.md"):
        review_docs.copy_docs(tmp_path / "review-docs", NAMES)

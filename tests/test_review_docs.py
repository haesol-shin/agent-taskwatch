"""The job copy of the shipped documents keeps the installed shape, so guide links resolve."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskwatch import review_docs


def test_the_job_copy_resolves_the_guide_links(tmp_path: Path):
    target = tmp_path / "review-docs"
    guides = review_docs.copy_docs(target)
    assert guides == review_docs.guide_dir(target)
    assert "(../checks.md" in (guides / "README.md").read_text(encoding="utf-8")
    assert (target / "agent-taskwatch" / "checks.md").is_file()
    assert review_docs.link_problems(target) == []


def test_a_missing_linked_document_is_reported(tmp_path: Path):
    target = tmp_path / "review-docs"
    review_docs.copy_docs(target)
    (target / "agent-taskwatch" / "checks.md").unlink()
    assert "agent-taskwatch/review/README.md: link does not resolve: ../checks.md" in (
        review_docs.link_problems(target)
    )


def test_a_link_that_leaves_the_job_copy_is_reported(tmp_path: Path):
    target = tmp_path / "review-docs"
    code = review_docs.copy_docs(target) / "code.md"
    code.write_text(
        code.read_text(encoding="utf-8") + "\n[out](../../../outside.md)\n", encoding="utf-8"
    )
    assert "agent-taskwatch/review/code.md: link leaves the tree: ../../../outside.md" in (
        review_docs.link_problems(target)
    )


def test_an_older_flat_copy_does_not_fail_the_job_copy(tmp_path: Path):
    target = tmp_path / "review-docs"
    target.mkdir()
    (target / "README.md").write_text("[checks](../checks.md)\n", encoding="utf-8")
    review_docs.copy_docs(target)
    assert review_docs.link_problems(target) == []


def test_a_job_keeps_the_copy_it_started_with(tmp_path: Path):
    target = tmp_path / "review-docs"
    common = review_docs.copy_docs(target) / "common.md"
    common.write_text("kept\n", encoding="utf-8")
    review_docs.copy_docs(target)
    assert common.read_text(encoding="utf-8") == "kept\n"


def test_a_missing_source_document_is_named(tmp_path: Path, monkeypatch):
    tree = tmp_path / "docs"
    (tree / "review").mkdir(parents=True)
    for name in review_docs.all_names():
        (tree / "review" / name).write_text("# Guide\n", encoding="utf-8")
    monkeypatch.setattr(review_docs, "docs_root", lambda: tree)
    with pytest.raises(review_docs.DocsError, match="checks.md"):
        review_docs.copy_docs(tmp_path / "review-docs")

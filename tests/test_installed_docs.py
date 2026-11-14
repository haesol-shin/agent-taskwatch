"""Installed docs must carry every file their relative Markdown links name."""

from __future__ import annotations

import posixpath
import tomllib
from pathlib import Path

from taskwatch import review_docs

ROOT = Path(__file__).resolve().parents[1]


def data_files(root: Path = ROOT) -> dict[str, list[str]]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["data-files"]


def install_map(root: Path, data: dict[str, list[str]]) -> dict[str, str]:
    """Map each installed path to the repository file it comes from."""
    installed: dict[str, str] = {}
    for destination, patterns in data.items():
        for pattern in patterns:
            matches = sorted(root.glob(pattern))
            assert matches, f"data-files pattern matches nothing: {pattern}"
            for source in matches:
                path = posixpath.join(destination, source.name)
                assert path not in installed, f"{path} is installed twice"
                installed[path] = source.relative_to(root).as_posix()
    return installed


def linked_targets(text: str) -> list[str]:
    """The local paths the Markdown links of a document name, without their anchors."""
    return [
        local
        for target in review_docs.link_targets(text)
        if (local := review_docs.local_target(target))
    ]


def missing_targets(root: Path, data: dict[str, list[str]]) -> list[str]:
    """Report every local link of an installed document whose target is not installed."""
    installed = install_map(root, data)
    missing: list[str] = []
    for path in sorted(installed):
        if not path.endswith(".md"):
            continue
        text = (root / installed[path]).read_text(encoding="utf-8")
        for local in linked_targets(text):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), local))
            if target not in installed:
                missing.append(f"{path}: target not installed: {local}")
    return missing


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_the_repository_installs_every_link_target_of_its_docs():
    assert missing_targets(ROOT, data_files()) == []


def test_a_link_target_outside_the_installed_set_fails(tmp_path: Path):
    write_files(
        tmp_path,
        {
            "docs/guides/README.md": "# Guide\n\nSee [rules](../rules.md#scope).\n",
            "docs/rules.md": "# Rules\n",
        },
    )
    data = {"share/docs/guides": ["docs/guides/*.md"]}
    assert missing_targets(tmp_path, data) == [
        "share/docs/guides/README.md: target not installed: ../rules.md"
    ]


def test_a_target_installed_at_the_linked_path_passes(tmp_path: Path):
    write_files(
        tmp_path,
        {
            "docs/guides/README.md": (
                "# Guide\n\nSee [rules](../rules.md), [other](other.md), [site](https://example.invalid).\n"
            ),
            "docs/guides/other.md": "# Other\n\n[top](#guide).\n",
            "docs/rules.md": "# Rules\n",
        },
    )
    data = {"share/docs": ["docs/*.md"], "share/docs/guides": ["docs/guides/*.md"]}
    assert missing_targets(tmp_path, data) == []


def test_the_job_copy_takes_exactly_the_installed_files():
    installed = set(install_map(ROOT, data_files()))
    assert all(path.startswith("share/") for path in installed)
    expected = {path.split("/", 1)[1] for path in installed}
    copied = {relative.as_posix() for _, relative in review_docs.shipped_files(ROOT / "docs")}
    assert copied == expected

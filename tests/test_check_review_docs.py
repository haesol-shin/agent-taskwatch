import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_review_docs.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_review_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECKER = load_checker()


def write_valid(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    sections = CHECKER.REQUIRED_SECTIONS
    for name, headings in sections.items():
        lines = [f"# {name}", ""]
        for heading in headings:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append("Text.")
            lines.append("")
        if name == "common.md":
            lines.append("**C-1.** One rule.")
            lines.append("")
        if name == "README.md":
            lines.append("See [common](common.md).")
            lines.append("")
        (root / name).write_text("\n".join(lines), encoding="utf-8")


def test_repo_review_docs_pass():
    assert CHECKER.check_review_dir(ROOT / "docs" / "review") == []


def test_valid_fixture_passes(tmp_path: Path):
    write_valid(tmp_path)
    assert CHECKER.check_review_dir(tmp_path) == []


def test_missing_section_fails(tmp_path: Path):
    write_valid(tmp_path)
    text = (tmp_path / "code.md").read_text(encoding="utf-8")
    (tmp_path / "code.md").write_text(text.replace("## Quality\n", ""), encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert "code.md: missing section: Quality" in errors


def test_missing_local_reference_fails(tmp_path: Path):
    write_valid(tmp_path)
    readme = tmp_path / "README.md"
    extra = "\n[gone](missing.md)\n"
    readme.write_text(readme.read_text(encoding="utf-8") + extra, encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert any("missing local reference: missing.md" in error for error in errors)


def test_angle_bracket_local_reference_fails(tmp_path: Path):
    write_valid(tmp_path)
    readme = tmp_path / "README.md"
    extra = "\n[gone](<missing.md>)\n"
    readme.write_text(readme.read_text(encoding="utf-8") + extra, encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert any("missing local reference: missing.md" in error for error in errors)


def test_reference_style_local_link_fails(tmp_path: Path):
    write_valid(tmp_path)
    readme = tmp_path / "README.md"
    extra = "\n[gone][ref]\n\n[ref]: missing.md\n"
    readme.write_text(readme.read_text(encoding="utf-8") + extra, encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert any("missing local reference: missing.md" in error for error in errors)


def test_angle_bracket_and_reference_links_pass(tmp_path: Path):
    write_valid(tmp_path)
    readme = tmp_path / "README.md"
    extra = (
        "\n[common](<common.md>)\n"
        "[web](<https://example.invalid/guide>)\n"
        "[code][code-ref]\n"
        "\n"
        "[code-ref]: code.md\n"
        "[site]: https://example.invalid/other\n"
    )
    readme.write_text(readme.read_text(encoding="utf-8") + extra, encoding="utf-8")
    assert CHECKER.check_review_dir(tmp_path) == []


def test_http_links_are_not_local(tmp_path: Path):
    write_valid(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n[web](https://example.invalid/guide)\n",
        encoding="utf-8",
    )
    assert CHECKER.check_review_dir(tmp_path) == []


def test_installed_tree_links_pass(tmp_path: Path):
    share = tmp_path / "share"
    (share / "agent-taskwatch" / "review").mkdir(parents=True)
    (share / "agent-taskwatch" / "checks.md").write_text("# Checks\n", encoding="utf-8")
    readme = share / "agent-taskwatch" / "review" / "README.md"
    readme.write_text("See [checks](../checks.md#scope).\n", encoding="utf-8")
    assert CHECKER.main(["--tree", str(share)]) == 0


def test_a_link_without_a_target_fails_the_tree_check(tmp_path: Path, capsys):
    share = tmp_path / "share"
    review = share / "agent-taskwatch" / "review"
    review.mkdir(parents=True)
    (review / "README.md").write_text("See [checks](../checks.md).\n", encoding="utf-8")
    assert CHECKER.main(["--tree", str(share)]) == 1
    assert "agent-taskwatch/review/README.md: link does not resolve: ../checks.md" in (
        capsys.readouterr().err
    )


def test_a_link_that_leaves_the_tree_fails_the_tree_check(tmp_path: Path, capsys):
    share = tmp_path / "share"
    review = share / "agent-taskwatch" / "review"
    review.mkdir(parents=True)
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")
    (review / "README.md").write_text("[out](../../../outside.md)\n", encoding="utf-8")
    assert CHECKER.main(["--tree", str(share)]) == 1
    assert "agent-taskwatch/review/README.md: link leaves the tree: ../../../outside.md" in (
        capsys.readouterr().err
    )


def test_rule_id_in_two_files_fails(tmp_path: Path):
    write_valid(tmp_path)
    code = tmp_path / "code.md"
    code.write_text(code.read_text(encoding="utf-8") + "\n**C-1.** Copied.\n", encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert any("C-1" in error and "appears in" in error for error in errors)


def test_duplicate_rule_id_in_one_file_fails(tmp_path: Path):
    write_valid(tmp_path)
    common = tmp_path / "common.md"
    common.write_text(common.read_text(encoding="utf-8") + "\n**C-1.** Again.\n", encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert "common.md: duplicate rule id: C-1" in errors


def rule_lines(folder: Path) -> dict[str, str]:
    found = {}
    for path in folder.glob("*.md"):
        if path.name == "MAPPING.md":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**") and ".**" in line:
                rule_id, _, text = line[2:].partition(".**")
                found[rule_id] = text.strip()
    return found


def test_mapping_names_only_rules_that_exist():
    rules = rule_lines(ROOT / "docs" / "review")
    mapping = (ROOT / "docs" / "review" / "MAPPING.md").read_text(encoding="utf-8")
    named = set(CHECKER.RULE_ID.findall(mapping))
    assert named and named <= rules.keys(), sorted(named - rules.keys())


def test_no_rule_text_is_repeated_across_the_guidelines():
    texts = list(rule_lines(ROOT / "docs" / "review").values())
    assert len(texts) == len(set(texts))


def test_renaming_the_design_question_or_scope_sections_fails(tmp_path: Path):
    for name in CHECKER.REQUIRED_FILES:
        text = (ROOT / "docs" / "review" / name).read_text(encoding="utf-8")
        text = text.replace("## Design questions", "## Design stuff")
        text = text.replace("## Scope and write targets", "## Writes")
        (tmp_path / name).write_text(text, encoding="utf-8")
    errors = CHECKER.check_review_dir(tmp_path)
    assert "common.md: missing section: Design questions" in errors
    assert "code.md: missing section: Scope and write targets" in errors


def test_the_design_question_and_scope_rules_exist():
    common = (ROOT / "docs" / "review" / "common.md").read_text(encoding="utf-8")
    code = (ROOT / "docs" / "review" / "code.md").read_text(encoding="utf-8")
    assert "**C-18.**" in common and "**K-19.**" in code

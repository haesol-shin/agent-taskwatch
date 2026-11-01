"""The reviewer launcher builds the omp command from an agent file and the guideline folder."""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskwatch import reviewer_launch
from taskwatch.job_runner import _guidelines
from taskwatch.review_docs import docs_dir, profile_names
from taskwatch.reviewer_launch import (
    LaunchError,
    _guideline_files,
    main,
    parse_agent,
    plan,
    system_prompt,
)

AGENT = """---
name: reviewer
description: "Read-only review"
tools: read, find, grep, bash
spawns: scout
model: "@slow"
thinking-level: high
autoloadSkills: [review-core]
output:
  properties:
    overall_correctness:
      enum: [correct, incorrect]
---

Find every problem. Read-only.
"""

FAKE_OMP = f"""#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:2] == ["agents", "unpack"], args
target = Path(args[args.index("--dir") + 1])
for name in ("task", "scout", "reviewer", "sonic"):
    (target / f"{{name}}.md").write_text(f"---\\nname: {{name}}\\ndescription: d\\n---\\nbody\\n")
print(json.dumps({{"total": 4}}))
"""


@pytest.fixture
def omp(tmp_path, monkeypatch) -> str:
    path = tmp_path / "bin" / "fake-omp"
    path.parent.mkdir()
    path.write_text(FAKE_OMP, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent-dir"))
    monkeypatch.chdir(tmp_path)
    return str(path)


@pytest.fixture
def guidelines(tmp_path) -> Path:
    folder = tmp_path / "guidelines"
    folder.mkdir()
    for name in (*profile_names("code"), "design.md", "source.md"):
        (folder / name).write_text(f"rule text of {name}\n", encoding="utf-8")
    return folder


def agent_file(tmp_path: Path, text: str = AGENT) -> Path:
    path = tmp_path / "reviewer.md"
    path.write_text(text, encoding="utf-8")
    return path


def command(capsys, *args: str) -> list[str]:
    assert main([*args, "--print-command"]) == 0
    return shlex.split(capsys.readouterr().out)


# --- agent definition -----------------------------------------------------------------------


def test_front_matter_gives_tools_spawns_model_and_body():
    agent = parse_agent(AGENT)
    assert agent.tools == ("read", "find", "grep", "bash")
    assert (agent.spawns, agent.models, agent.thinking) == (("scout",), ("@slow",), "high")
    assert agent.body == "Find every problem. Read-only.\n"


@pytest.mark.parametrize(
    "text, spawns",
    [("spawns: '*'\n", None), ("spawns: [scout, spec]\n", ("scout", "spec")), ("", ())],
)
def test_spawns_forms(text, spawns):
    assert parse_agent(f"---\nname: r\ntools: []\n{text}---\nbody\n").spawns == spawns


@pytest.mark.parametrize(
    "text", ["no front matter\n", "---\nname: r\nbody\n", "---\ntools: read\n---\nx\n"]
)
def test_a_definition_without_usable_front_matter_is_refused(text):
    with pytest.raises(LaunchError):
        parse_agent(text)


# --- the command ----------------------------------------------------------------------------


def built(tmp_path, agent_text=AGENT, layer=None):
    agent = parse_agent(agent_text)
    files = [("common.md", "common text\n"), ("code.md", "code text\n")]
    return plan(agent, files, layer, "omp", tmp_path / "work")


def test_prompt_is_body_then_each_guideline_in_full_then_the_layer():
    agent = parse_agent(AGENT)
    text = system_prompt(agent, [("common.md", "one\n"), ("code.md", "two\n")], "layer\n")
    assert text.index("Find every problem") < text.index("one") < text.index("two")
    assert text.index("two") < text.index("layer")
    assert '<guideline file="common.md">\none\n</guideline>' in text
    assert "review-core" not in text


def test_editing_a_guideline_changes_the_next_command_and_files(tmp_path):
    first = plan(parse_agent(AGENT), [("c.md", "v1\n")], None, "omp", tmp_path)
    again = plan(parse_agent(AGENT), [("c.md", "v1\n")], None, "omp", tmp_path)
    second = plan(parse_agent(AGENT), [("c.md", "v2\n")], None, "omp", tmp_path)
    assert first.argv == again.argv
    assert first.system_path != second.system_path
    reviewer_launch.materialize(second)
    assert "v2" in second.system_path.read_text(encoding="utf-8")


# --- the program ----------------------------------------------------------------------------


def test_main_without_print_command_replaces_itself_with_omp(
    tmp_path, guidelines, omp, monkeypatch
):
    seen = []
    monkeypatch.setattr(os, "execvp", lambda file, argv: seen.append((file, argv)))
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    assert main([*args, "--work-dir", str(tmp_path / "work"), "-p", "go"]) == 0
    assert seen[0][0] == omp and seen[0][1][1:3] == ["-p", "go"]


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--review", "poetry"], "unknown review"),
        (["--guidelines", "{tmp}/nowhere"], "unreadable"),
        (["--agent", "{tmp}/missing.md"], "unreadable"),
    ],
)
def test_bad_input_stops_before_omp_runs(
    tmp_path, guidelines, omp, capsys, monkeypatch, extra, message
):
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("omp must not start"))
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    args += [part.replace("{tmp}", str(tmp_path)) for part in extra]
    assert main(args) == 1
    assert message in capsys.readouterr().err


def test_the_shipped_guidelines_and_launch_layers_load_for_every_review():
    folder = docs_dir("review")
    for review in ("code", "design", "source"):
        names = [name for name, _ in _guideline_files(folder, [review])]
        assert names == list(profile_names(review))
    assert [n for n, _ in _guideline_files(folder, [])][-3:] == [
        "code.md",
        "design.md",
        "source.md",
    ]
    for layer in ("taskwatch.md", "omp-alone.md"):
        assert (docs_dir("launch") / layer).read_text(encoding="utf-8").strip()


def test_the_job_copy_and_the_source_folder_give_the_same_guideline_text(tmp_path):
    copy = tmp_path / "job"
    copy.mkdir()
    _guidelines(SimpleNamespace(review="code", outputs=()), copy)  # type: ignore[arg-type]
    source = _guideline_files(docs_dir("review"), ["code"])
    assert _guideline_files(copy / "review-docs", ["code"]) == source


BLOCK_AGENT = """---
name: reviewer
tools:
  - read
  - grep
spawns:
- scout
model:
  - "@slow"
---
body
"""


def test_block_lists_from_omp_unpack_are_read():
    agent = parse_agent(BLOCK_AGENT)
    assert (agent.tools, agent.spawns, agent.models) == (("read", "grep"), ("scout",), ("@slow",))


@pytest.mark.parametrize(
    "text", ["tools:\nspawns: scout\n", "tools: |\n  read\n", "tools: read\nmodel:\n"]
)
def test_a_list_the_launcher_cannot_read_is_refused_not_dropped(text):
    with pytest.raises(LaunchError, match="no value"):
        parse_agent(f"---\nname: r\n{text}---\nbody\n")


def test_missing_tools_is_refused_and_empty_tools_means_no_tools(tmp_path):
    with pytest.raises(LaunchError, match="must list its tools"):
        parse_agent("---\nname: r\n---\nbody\n")
    none = built(tmp_path, "---\nname: r\ntools: []\nspawns: []\n---\nbody\n")
    assert "--no-tools" in none.argv and "--tools" not in none.argv
    spawning = built(tmp_path, "---\nname: r\ntools: []\nspawns: scout\n---\nbody\n")
    assert spawning.argv[list(spawning.argv).index("--tools") + 1] == "task"


def test_command_carries_tools_model_prompt_overlay_and_the_only_extension(tmp_path):
    built_plan = built(tmp_path)
    argv = list(built_plan.argv)
    assert argv[argv.index("--model") + 1] == "@slow"
    assert argv[argv.index("--thinking") + 1] == "high"
    # Spawning needs the task tool even when the definition does not list it.
    assert argv[argv.index("--tools") + 1] == "read,find,grep,bash,task"
    assert argv[argv.index("--trusted-extension") + 1] == str(built_plan.extension_path)
    assert argv[argv.index("--append-system-prompt") + 1] == str(built_plan.system_path)
    assert argv[-2:] == ["--config", str(built_plan.overlay_path)]
    assert built_plan.overlay == "async:\n  enabled: false\n"
    assert "--no-extensions" not in argv and built_plan.extension_path.is_absolute()


def test_extension_holds_the_tool_and_spawn_lists(tmp_path):
    text = built(tmp_path).extension
    assert '["bash", "find", "grep", "read", "task"]' in text
    assert 'const AGENTS = ["scout"];' in text
    wildcard = built(tmp_path, AGENT.replace("spawns: scout", "spawns: '*'")).extension
    assert "const AGENTS = null;" in wildcard
    none = built(tmp_path, AGENT.replace("spawns: scout\n", "")).extension
    assert "const AGENTS = [];" in none and '"task"' not in none.split("const AGENTS")[0]


BUN = shutil.which("bun") or str(Path.home() / ".bun" / "bin" / "bun")


@pytest.mark.skipif(not Path(BUN).exists(), reason="bun is not installed")
def test_generated_extension_blocks_what_the_lists_do_not_allow(tmp_path):
    ext = built(tmp_path).extension
    path = tmp_path / "enforce.mjs"
    path.write_text(ext, encoding="utf-8")
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        f"""import ext from {json.dumps(str(path))};
const on = {{}};
ext({{ on: (name, fn) => {{ on[name] = fn; }} }});
const main = {{ agent: {{ kind: "main" }} }};
const sub = {{ agent: {{ kind: "sub" }} }};
console.log(JSON.stringify([
  await on.tool_call({{ toolName: "read" }}, main),
  (await on.tool_call({{ toolName: "write" }}, main)).block,
  (await on.tool_call({{ toolName: "mcp_x" }}, main)).block,
  await on.tool_call({{ toolName: "write" }}, sub),
  await on.before_subagent_spawn({{ agent: "scout" }}),
  (await on.before_subagent_spawn({{ agent: "task" }})).block,
]));
""",
        encoding="utf-8",
    )
    out = subprocess.run([BUN, str(probe)], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [None, True, True, None, None, True]


@pytest.mark.parametrize(
    "extra", [["--config", "x.yml"], ["--tools=edit"], ["--no-tools"], ["-e", "ext.ts"]]
)
def test_options_that_would_widen_the_review_are_refused(
    tmp_path, guidelines, omp, capsys, monkeypatch, extra
):
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("omp must not start"))
    args = ["--agent", str(agent_file(tmp_path)), "--guidelines", str(guidelines), "--omp", omp]
    assert main([*args, "--work-dir", str(tmp_path / "w"), *extra]) == 1
    assert "would widen" in capsys.readouterr().err

"""Build the omp reviewer command at run time and run it.

The omp agent definition says who the reviewer is: its tools, the agents it may spawn and its
model come from the front matter, and its body is the start of the appended system prompt.
The guideline folder follows it, file by file, then an optional launch-path layer. Nothing is
copied into this module, so editing a source file changes the next run.

An overlay turns async off, because ``omp -p`` and RPC runs end before a background sub-agent
reports. A generated extension, loaded as the only extension, blocks inside omp every tool call
outside the definition's tool list and every spawn of an agent outside its ``spawns``. The
prompt cannot enforce those lists, and ``--tools`` does not cover custom tools or MCP servers;
the extension covers whatever source a tool or an agent comes from.

Options not listed here pass through to omp, before the built options, so they cannot widen
the tools, the extensions, the overlay or the system prompt; those that would are refused.
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .review_docs import PROFILES, all_names, docs_dir, profile_names

_KEY = re.compile(r"^([A-Za-z][\w-]*):[ \t]*(.*)$")
_THINKING = ("thinking-level", "thinkingLevel", "thinking")
_BLOCK_ITEM = re.compile(r"^[ \t]*-[ \t]+(.*\S)[ \t]*$")
_READ_KEYS = frozenset({"tools", "spawns", "model", *_THINKING})
# Options that would add tools, extensions or agents the definition did not allow.
_REFUSED = frozenset(
    {
        "--tools",
        "--no-tools",
        "--config",
        "-e",
        "--extension",
        "--hook",
        "--plugin-dir",
    }
    | {"--trusted-extension", "--append-system-prompt", "--system-prompt"}
)


class LaunchError(Exception):
    """The command cannot be built; nothing was started."""


@dataclass(frozen=True, slots=True)
class Agent:
    body: str
    tools: tuple[str, ...]  # empty: no built-in tool
    spawns: tuple[str, ...] | None  # None: any agent; empty: none
    models: tuple[str, ...]
    thinking: str


@dataclass(frozen=True, slots=True)
class Plan:
    argv: tuple[str, ...]
    system: str
    overlay: str
    extension: str
    system_path: Path
    overlay_path: Path
    extension_path: Path


def _items(raw: str) -> tuple[str, ...]:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts = (part.strip().strip("'\"").strip() for part in text.split(","))
    return tuple(part for part in parts if part)


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise LaunchError("the agent definition has no front matter")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            break
    else:
        raise LaunchError("the agent definition front matter is not closed")
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:end], start=1):
        match = _KEY.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key in _READ_KEYS and value == "":
            # A block list: the dash lines that follow. Other nested blocks are not read.
            items = []
            for follow in lines[index + 1 : end]:
                item = _BLOCK_ITEM.match(follow)
                if not item:
                    break
                items.append(item.group(1))
            value = ", ".join(items)
        if key in _READ_KEYS and (value == "" or value[0] in "|>&*!"):
            raise LaunchError(f"front matter `{key}` has no value this launcher can read")
        fields[key] = value
    return fields, "\n".join(lines[end + 1 :]).strip() + "\n"


def parse_agent(text: str) -> Agent:
    fields, body = _front_matter(text)
    if not fields.get("name", "").strip():
        raise LaunchError("the agent definition needs a name")
    if "tools" not in fields:
        # An omitted list means omp's default tools, including edit and write.
        raise LaunchError("the agent definition must list its tools (`tools: []` for none)")
    spawns = _items(fields.get("spawns", ""))
    return Agent(
        body=body,
        tools=_items(fields["tools"]),
        spawns=None if spawns == ("*",) else spawns,
        models=_items(fields.get("model", "")),
        thinking=next((v[0] for v in (_items(fields.get(k, "")) for k in _THINKING) if v), ""),
    )


def agent_name(text: str) -> str:
    return _front_matter(text)[0].get("name", "").strip().strip("'\"")


def _guideline_files(folder: Path, reviews: Sequence[str]) -> list[tuple[str, str]]:
    names = list(dict.fromkeys(n for review in reviews for n in profile_names(review))) or list(
        all_names()
    )
    rows = []
    for name in names:
        path = folder / name
        try:
            rows.append((name, path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise LaunchError(f"guideline file is unreadable: {path}: {exc}") from exc
    return rows


def system_prompt(agent: Agent, guidelines: Iterable[tuple[str, str]], layer: str | None) -> str:
    parts = [
        agent.body.rstrip(),
        "The review guidelines follow in full, one file after another. They are already read:"
        " do not look for them in a skill or on disk.",
    ]
    parts += [
        f'<guideline file="{name}">\n{text.rstrip()}\n</guideline>' for name, text in guidelines
    ]
    if layer is not None:
        parts.append(f"<launch-layer>\n{layer.rstrip()}\n</launch-layer>")
    return "\n\n".join(parts) + "\n"


OVERLAY = "async:\n  enabled: false\n"


def extension_text(tools: Sequence[str], spawns: Sequence[str] | None) -> str:
    """An extension that enforces the lists inside omp, whatever tool or agent source exists.

    A tool call outside the list and a spawn of an agent outside it are blocked there, so
    custom tools, MCP servers, plugins and extra agents cannot join the review.
    """
    return _EXTENSION.format(tools=json.dumps(sorted(tools)), agents=json.dumps(spawns))


_EXTENSION = """// Generated by taskwatch.reviewer_launch. Do not edit.
const TOOLS = {tools};
const AGENTS = {agents};
export default function (pi) {{
  pi.on("tool_call", async (event, ctx) => {{
    if (ctx && ctx.agent && ctx.agent.kind !== "main") return;
    if (!TOOLS.includes(event.toolName)) {{
      const reason = `tool ${{event.toolName}} is not in the reviewer's tool list`;
      return {{ block: true, reason }};
    }}
  }});
  pi.on("before_subagent_spawn", async (event) => {{
    if (AGENTS !== null && !AGENTS.includes(event.agent)) {{
      const reason = `agent ${{event.agent}} is not in the reviewer's spawns`;
      return {{ block: true, reason }};
    }}
  }});
}}
"""


def plan(
    agent: Agent,
    guidelines: Iterable[tuple[str, str]],
    layer: str | None,
    omp: str,
    work_dir: Path,
) -> Plan:
    system = system_prompt(agent, guidelines, layer)
    tools = list(agent.tools)
    if agent.spawns != () and "task" not in tools:
        tools.append("task")
    extension = extension_text(tools, agent.spawns)
    digest = hashlib.sha256(f"{system}\0{OVERLAY}\0{extension}".encode()).hexdigest()[:16]
    folder = work_dir.resolve() / digest
    system_path, overlay_path = folder / "system.md", folder / "overlay.yml"
    extension_path = folder / "enforce.mjs"
    argv = [omp]
    if agent.models:
        argv += ["--model", agent.models[0]]
    if agent.thinking:
        argv += ["--thinking", agent.thinking]
    argv += ["--tools", ",".join(tools)] if tools else ["--no-tools"]
    argv += ["--trusted-extension", str(extension_path), "--append-system-prompt", str(system_path)]
    argv += ["--config", str(overlay_path)]
    return Plan(tuple(argv), system, OVERLAY, extension, system_path, overlay_path, extension_path)


def materialize(built: Plan) -> None:
    """Write the files under a name that holds their content hash, so a rerun is a no-op."""
    files = (
        (built.system_path, built.system),
        (built.overlay_path, built.overlay),
        (built.extension_path, built.extension),
    )
    for path, text in files:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.name}.{os.getpid()}")
        part.write_text(text, encoding="utf-8")
        os.replace(part, path)


def _parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="python -m taskwatch.reviewer_launch", allow_abbrev=False, description=__doc__
    )
    cli.add_argument("--agent", type=Path, required=True, help="omp agent definition file")
    cli.add_argument(
        "--guidelines", type=Path, help="guideline folder (default: the installed one)"
    )
    cli.add_argument(
        "--review", action="append", default=[], help="review (code, design, source); default all"
    )
    cli.add_argument("--layer", type=Path, help="launch-path layer file appended last")
    cli.add_argument("--omp", default="omp", help="omp executable")
    cli.add_argument("--work-dir", type=Path, help="where the prompt and overlay files are written")
    cli.add_argument("--print-command", action="store_true", help="print the command, do not run")
    return cli


def build(args: argparse.Namespace) -> Plan:
    folder = args.guidelines or docs_dir("review")
    if folder is None:
        raise LaunchError("review guidelines were not found")
    try:
        text = args.agent.read_text(encoding="utf-8")
        layer = args.layer.read_text(encoding="utf-8") if args.layer else None
    except OSError as exc:
        raise LaunchError(f"input is unreadable: {exc}") from exc
    unknown = [review for review in args.review if review not in PROFILES]
    if unknown:
        raise LaunchError(f"unknown review: {', '.join(unknown)}")
    agent = parse_agent(text)
    work = args.work_dir or Path(tempfile.gettempdir()) / "taskwatch-reviewer"
    return plan(agent, _guideline_files(folder, args.review), layer, args.omp, work)


_STDERR_LIMIT = 64 * 1024
_DRAIN_AFTER_EXIT = 1.0
_CANCEL_GRACE = 1.0
_FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
# The two startup rejections of omp's tool selection, at the start of a line: an unknown name in
# `--tools`, and a built-in tool that its settings disable. Runtime tool errors do not match.
_TOOL_REJECTION = re.compile(
    rb"^(?:Error:[ \t]*)?"
    rb"(?:Unknown tools?\b[^\r\n]{0,40}:|Built-in tool unavailable[^\r\n]{0,40}:)",
    re.IGNORECASE | re.MULTILINE,
)


def _die_with_parent() -> None:
    """Run in the child before exec: a killed launcher must not leave omp running."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG


def _children() -> set[int]:
    """Direct children of this launcher thread, including subreaper adoptions."""
    return {int(pid) for pid in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()}


class _Run:
    """One supervised omp run: its own process group, stderr relay and cancellation."""

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.proc: subprocess.Popen | None = None
        self.cancelled = 0
        self.kill_at: float | None = None
        self.excerpt = bytearray()
        self.rejection = b""

    def send(self, signum: int) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signum)
            except ProcessLookupError:
                pass

    def forward(self, signum: int, _frame: object) -> None:
        self.cancelled = signum
        self.kill_at = time.monotonic() + _CANCEL_GRACE
        self.send(signum)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.command,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_die_with_parent,
        )
        if self.cancelled:
            self.send(self.cancelled)

    def relay(self) -> None:
        """Copy stderr as it arrives. After omp exits, leftover holders get a short drain."""
        assert self.proc is not None and self.proc.stderr is not None
        exited = 0.0
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stderr, selectors.EVENT_READ)
            while selector.get_map() or self.proc.poll() is None:
                if self.kill_at is not None and time.monotonic() >= self.kill_at:
                    self.send(signal.SIGKILL)
                    self.kill_at = None
                if self.proc.poll() is not None:
                    self.send(signal.SIGKILL)
                    if not exited:
                        self.reap_adopted()
                        exited = time.monotonic()
                    if time.monotonic() - exited > _DRAIN_AFTER_EXIT:
                        return
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 16384)
                    if chunk:
                        self.copy(chunk)
                    else:
                        selector.unregister(key.fileobj)

    def copy(self, chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            view = view[os.write(sys.stderr.fileno(), view) :]
        self.excerpt.extend(chunk)
        del self.excerpt[:-_STDERR_LIMIT]
        match = _TOOL_REJECTION.search(self.excerpt)
        if match:
            # Keep the named rejection and its valid-tools list at the end of the eventual
            # error, inside the tail that the RPC client records.
            start = max(0, match.start() - 512)
            self.rejection = bytes(self.excerpt[start : match.start() + 8192])

    def reap(self) -> None:
        """Kill the group, then collect adopted descendants even if they changed session."""
        if self.proc is None:
            return
        self.send(signal.SIGKILL)
        self.proc.wait()
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        self.reap_adopted()

    def reap_adopted(self) -> None:
        # This standalone launcher owns all of its children. An exiting child can
        # make /proc's children list omit a live sibling, so only ECHILD from the
        # kernel is proof that cleanup is complete, never an empty /proc snapshot.
        while True:
            try:
                exited = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return
            if exited is not None:
                os.waitpid(exited.si_pid, 0)
                continue
            for pid in _children():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def _supervise(command: list[str], agent_path: Path, required: str) -> int:
    """Keep stdout inherited and own only this invocation's process group and stderr."""
    # A dead omp can leave children behind. Adopt them so killing the owned group also
    # reaps them, rather than leaving zombies for the caller's service manager.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:  # PR_GET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot read child-subreaper state")
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot own reviewer descendants")
    run = _Run(command)
    handlers = {}
    try:
        for signum in _FORWARDED:
            handlers[signum] = signal.signal(signum, run.forward)
        run.start()
        run.relay()
        assert run.proc is not None
        status = run.proc.wait()
        if status and run.rejection:
            print(_tool_advice(agent_path, required, run.rejection), file=sys.stderr)
    finally:
        run.reap()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        libc.prctl(36, previous.value, 0, 0, 0)
    terminating = run.cancelled or (-status if status < 0 else 0)
    if terminating:
        if terminating not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(terminating, signal.SIG_DFL)
        os.kill(os.getpid(), terminating)
    return status


def _tool_advice(agent_path: Path, required: str, rejection: bytes) -> str:
    return (
        f"\nreviewer_launch: omp rejected required tools for --agent {agent_path}; "
        f"effective required tools: {required or '(none)'}. "
        "Enable the named tool in the effective omp configuration, or correct "
        "the agent definition to declare the tools the review actually requires. "
        "Tools and grants were not changed.\n" + rejection.decode("utf-8", errors="replace")
    )


def main(argv: list[str] | None = None) -> int:
    args, passthrough = _parser().parse_known_args(argv)
    try:
        built = build(args)
    except (LaunchError, KeyError, OSError) as exc:
        print(f"reviewer_launch: {exc}", file=sys.stderr)
        return 1
    refused = [arg for arg in passthrough if arg.split("=", 1)[0] in _REFUSED]
    if refused:
        print(f"reviewer_launch: option would widen the review: {refused[0]}", file=sys.stderr)
        return 1
    materialize(built)
    # Built options go last: omp lets a later option and a later overlay win.
    command = [built.argv[0], *passthrough, *built.argv[1:]]
    if args.print_command:
        print(shlex.join(command))
        return 0
    try:
        required = built.argv[built.argv.index("--tools") + 1] if "--tools" in built.argv else ""
        return _supervise(command, args.agent, required)
    except OSError as exc:
        print(f"reviewer_launch: cannot run {command[0]}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

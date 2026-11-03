"""Print mode: a review command that answers once, not over omp RPC.

The command (`codex exec`, `claude -p`, `gemini -p`, or any program) gets the review message on
stdin and prints a verdict that follows the JSON Schema in `docs/launch/review-verdict.schema.json`.
The verdict is found in the last-message file named by `{output}`, then in stdout, as plain JSON,
inside a wrapper object (for example the `result` of `claude -p --output-format json`), in a code
fence, or as the last JSON line of a stream. It is validated against the schema and turned into
the verdict line and finding lines that `executor` reads, so one parser decides what is accepted.
Nothing here calls a model.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .rate_limit import RateLimit, print_rate_limit, scrub

SCHEMA_NAME = "review-verdict.schema.json"
WRAPPER_KEYS = (
    "structured_output",
    "result",
    "response",
    "output",
    "message",
    "content",
    "text",
    "last_message",
)
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)
_DEPTH = 6
MAX_ERRORS = 5
KEEP_CHARS = 20000
STDERR_CHARS = 4000


class ReplyError(ValueError):
    """The review command ran, but its reply holds no acceptable verdict."""


class RunError(Exception):
    """The review command could not run to the end: exit code, timeout.

    `rate_limit` is set when a failed command's standard error holds an explicit HTTP 429."""

    def __init__(self, message: str = "", rate_limit: RateLimit | None = None) -> None:
        super().__init__(message)
        self.rate_limit = rate_limit


# --- run -----------------------------------------------------------------------------------


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _feed(proc: subprocess.Popen, text: str) -> None:
    try:
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass  # the command did not read its input; its exit code says what happened


def run(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    message: str,
    *,
    timeout: float,
    every: float,
    tick: Callable[[], None],
    abort: threading.Event,
    stderr_path: Path,
) -> str:
    """Run the command with `message` on stdin; return its stdout.

    `tick` is called every `every` seconds while it lives. An `abort` flag kills the process
    group and returns the output so far; the caller decides what that means. A nonzero exit
    code or a timeout raises RunError.
    """
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        chunks: list[bytes] = []
        assert proc.stdout is not None
        reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
        writer = threading.Thread(target=_feed, args=(proc, message), daemon=True)
        reader.start()
        writer.start()
        deadline = time.monotonic() + timeout
        last = time.monotonic()
        try:
            while proc.poll() is None:
                if abort.is_set():
                    _kill(proc)
                    break
                now = time.monotonic()
                if now >= deadline:
                    _kill(proc)
                    proc.wait()
                    raise RunError(f"the review command did not finish within {timeout:g} seconds")
                if now - last >= every:
                    tick()
                    last = now
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                _kill(proc)
            proc.wait()
            reader.join(5)
    out = b"".join(chunks).decode("utf-8", "replace")
    if proc.returncode != 0 and not abort.is_set():
        detail = _tail(stderr_path)
        limit = print_rate_limit(_tail(stderr_path, STDERR_CHARS))
        raise RunError(
            f"the review command exited with code {proc.returncode}: "
            f"{scrub(detail) if limit else detail}",
            limit,
        )
    return out


def _tail(path: Path, size: int = 400) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-size:]


# --- find ----------------------------------------------------------------------------------


def _find(value: object, depth: int = 0) -> dict | None:
    """The verdict object inside `value`, or None. The newest candidate wins in a list."""
    if depth > _DEPTH:
        return None
    if isinstance(value, dict):
        if "verdict" in value:
            return value
        for key in WRAPPER_KEYS:
            if key in value and (found := _find(value[key], depth + 1)) is not None:
                return found
        return None
    if isinstance(value, list):
        for item in reversed(value):
            if (found := _find(item, depth + 1)) is not None:
                return found
        return None
    if isinstance(value, str):
        return _find_text(value, depth)
    return None


def _find_text(text: str, depth: int) -> dict | None:
    text = text.strip()
    if fence := _FENCE.match(text):
        text = fence.group(1).strip()
    if not text.startswith(("{", "[")):
        return None if "\n" not in text else _find_lines(text, depth)
    try:
        return _find(json.loads(text), depth + 1)
    except ValueError:
        return _find_lines(text, depth)


def _find_lines(text: str, depth: int) -> dict | None:
    """A stream: one JSON value per line, the last one that holds a verdict wins."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            found = _find(json.loads(line), depth + 1)
        except ValueError:
            continue
        if found is not None:
            return found
    return None


# --- validate ------------------------------------------------------------------------------

_TYPES: dict[str, Callable[[object], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def validate(value: object, schema: Mapping, root: Mapping | None = None, path: str = "$") -> list:
    """Errors of `value` against `schema`: the keywords `type`, `enum`, `required`, `properties`,
    `additionalProperties: false`, `items`, `pattern` and a local `$ref` to `#/$defs/<name>`.
    Any other keyword is not checked; the schema file uses no other."""
    root = root or schema
    if "$ref" in schema:
        name = str(schema["$ref"]).removeprefix("#/$defs/")
        return validate(value, root["$defs"][name], root, path)
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_TYPES[name](value) for name in names):
            return [f"{path}: expected {' or '.join(names)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        errors += _object_errors(value, schema, root, path)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += validate(item, schema["items"], root, f"{path}[{index}]")
    return errors


def _object_errors(value: dict, schema: Mapping, root: Mapping, path: str) -> list[str]:
    errors = [f"{path}: missing {key}" for key in schema.get("required", ()) if key not in value]
    properties = schema.get("properties", {})
    for key, item in value.items():
        if key in properties:
            errors += validate(item, properties[key], root, f"{path}.{key}")
        elif schema.get("additionalProperties") is False:
            errors.append(f"{path}: unexpected {key}")
    return errors


# --- turn into the lines the parser reads ---------------------------------------------------


def render(verdict: Mapping) -> str:
    """The verdict line, then each finding: heading, finding line, detail. The line carries
    only what the parser reads; empty `summary` and a null `design_check` are left out."""
    line = {k: verdict[k] for k in ("verdict", "blocking", "checked", "not_checked")}
    if verdict.get("design_check") is not None:
        line["design_check"] = verdict["design_check"]
    out = [json.dumps(line, ensure_ascii=False)]
    for finding in verdict["findings"]:
        item = {k: finding[k] for k in ("finding", "severity", "category", "rule", "location")}
        if finding["summary"].strip():
            item["summary"] = finding["summary"]
        place = " ".join(finding["location"].split())
        out += ["", f"### {finding['finding']} {place}"]
        out.append(json.dumps(item, ensure_ascii=False))
        out += [f"> {text}" for text in finding["detail"].splitlines()]
    return "\n".join(out)


def verdict_body(replies: Sequence[tuple[str, str]], schema: Mapping) -> str:
    """The review text of the first reply that holds a verdict. `replies` is (where, text),
    the last-message file first. Raises ReplyError naming the first schema errors."""
    for where, text in replies:
        found = _find(text)
        if found is None:
            continue
        errors = validate(found, schema)
        if errors:
            shown = "; ".join(errors[:MAX_ERRORS])
            more = f" (+{len(errors) - MAX_ERRORS} more)" if len(errors) > MAX_ERRORS else ""
            raise ReplyError(f"the verdict in the {where} does not match the schema: {shown}{more}")
        return render(found)
    places = ", ".join(where for where, _ in replies)
    raise ReplyError(f"no verdict JSON was found in the {places} of the review command")

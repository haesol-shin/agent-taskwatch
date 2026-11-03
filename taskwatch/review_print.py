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
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

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
# Whole JSON documents and individual JSONL candidates have the same explicit bound.
MAX_REPLY_BYTES = 4 * 1024 * 1024


def excerpt(path: Path, size: int = KEEP_CHARS) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(size).decode("utf-8", "replace")
    except OSError:
        return ""


def _file_verdict(path: Path) -> dict | None:
    with path.open("rb") as stream:
        data = stream.read(MAX_REPLY_BYTES + 1)
        if len(data) <= MAX_REPLY_BYTES:
            return _find(data.decode("utf-8", "replace"))
        stream.seek(0)
        found = None
        for candidate in _reply_candidates(stream):
            if (verdict := _find_text(candidate.decode("utf-8", "replace"), 0)) is not None:
                found = verdict
        return found


def _reply_candidates(stream: BinaryIO) -> Iterator[bytes]:
    """Frame multiline documents before JSONL discovery, without retaining an entire stream."""
    candidate = bytearray()
    depth = 0
    quoted = escaped = fenced = False
    while line := stream.readline(MAX_REPLY_BYTES + 1):
        if len(candidate) + len(line) > MAX_REPLY_BYTES:
            raise ReplyError("review reply candidate exceeds the 4 MiB limit")
        stripped = line.strip()
        if not candidate:
            fenced = stripped.startswith(b"```")
            if not fenced and not stripped.startswith((b"{", b"[")):
                continue
        candidate.extend(line)
        if fenced:
            if stripped != b"```" or len(candidate) == len(line):
                continue
        else:
            depth, quoted, escaped = _json_balance(line, depth, quoted, escaped)
            if depth > 0 or quoted:
                continue
        yield bytes(candidate)
        candidate.clear()
        depth = 0
        quoted = escaped = fenced = False
    if candidate:
        yield bytes(candidate)


def _json_balance(data: bytes, depth: int, quoted: bool, escaped: bool) -> tuple[int, bool, bool]:
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
        elif byte in (125, 93):
            depth -= 1
    return depth, quoted, escaped


def verdict_files(replies: Sequence[tuple[str, Path]], schema: Mapping) -> str:
    for where, path in replies:
        if not path.is_file():
            continue
        found = _file_verdict(path)
        if found is not None:
            return verdict_body([(where, json.dumps(found))], schema)
    places = ", ".join(where for where, _ in replies)
    raise ReplyError(f"no verdict JSON was found in the {places} of the review command")


class ReplyError(ValueError):
    """The review command ran, but its reply holds no acceptable verdict."""


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

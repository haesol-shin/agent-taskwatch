"""`choices.json` of a design job: the picks the owner answers, and the check on its shape.

The schema and the four tests that make a choice a pick are in docs/jobs/design.md. The check
here is plain code. It cannot tell whether a choice that is listed as decided should have been
a pick; the reviewer's `choices` key and rule D-9 judge that.
"""

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

from .job_types import CHOICES_FILE as FILE
from .job_types import builds_design
from .jobs import Jobs, JobState
from .pointers import pointer_problem, pointers
from .review_docs import docs_dir
from .review_print import MAX_ERRORS, validate

DEFAULTS_FILE = "defaults.json"
DEFAULTS_SCHEMA = "defaults.schema.json"
MAX_PICKS = 5
MIN_SEARCHES = 3
REPLY = "reply: ok | <id>=<key|text> ... | free text"
AWAITING = "awaiting owner"
_REVERSIBLE = ("cheap", "costly")
_PICKS_LINE = re.compile(r"^(?:Gate [^:\n]*: )?PICKS:[ \t]*(.*)$", re.MULTILINE)
_WRITER = re.compile(r"^(#{1,6})\s+Writer choices\s*$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6})\s")


def is_design(job: JobState) -> bool:
    return builds_design(job.review, job.review_target)


def load(path: Path) -> tuple[dict | None, list[str]]:
    """The parsed file and the reasons it cannot be used."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"required file is missing: {path.name}"]
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, [f"{path.name} is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"{path.name} must be a JSON object"]
    return data, []


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def problems(
    workspace: Path,
    roots: tuple[Path, ...],
    outputs: tuple[str, ...],
    *,
    defaults_path: Path | None = None,
) -> list[str]:
    """Why `choices.json` in the workspace fails the schema; empty when it passes."""
    data, issues = load(workspace / FILE)
    if data is None:
        return issues
    for key in ("picks", "decided", "existing_tools"):
        if not isinstance(data.get(key), list):
            issues.append(f"{FILE}: `{key}` must be a list")
    if issues:
        return issues
    seen: set[str] = set()
    issues += _picks(data["picks"], roots, seen)
    issues += _decided(data["decided"], roots, seen)
    issues += _tools(data["existing_tools"])
    applied = data.get("defaults_applied", [])
    if not isinstance(applied, list) or not all(_text(item) for item in applied):
        issues.append(f"{FILE}: `defaults_applied` must be a list of nonempty strings")
    elif defaults_path is not None:
        target = roots[-1] if roots else workspace
        issues += _default_problems(defaults_path, target, data["picks"], applied)
    issues += _writer_choices(workspace, outputs, data["decided"])
    return issues


def _picks(picks: list, roots: tuple[Path, ...], seen: set[str]) -> list[str]:
    if len(picks) > MAX_PICKS:
        return [f"{FILE}: at most {MAX_PICKS} picks; record the rest as decided with deferred"]
    issues = []
    for index, pick in enumerate(picks):
        issues += _pick(pick, index, roots, seen)
    return issues


def _pick(pick: object, index: int, roots: tuple[Path, ...], seen: set[str]) -> list[str]:
    if not isinstance(pick, dict) or not _text(pick.get("id")):
        return [f"{FILE}: pick {index} needs an id"]
    name = f"pick {pick['id']}"
    issues = _unique(pick["id"], seen)
    if not _text(pick.get("question")) or not _text(pick.get("why")):
        issues.append(f"{FILE}: {name} needs a question and a why")
    options = pick.get("options")
    if not isinstance(options, list) or len(options) < 2:
        return [*issues, f"{FILE}: {name} needs at least 2 options"]
    keys = []
    for option in options:
        if not isinstance(option, dict) or not _text(option.get("key")):
            issues.append(f"{FILE}: {name} has an option without a key")
            continue
        keys.append(option["key"])
        if not _text(option.get("label")) or not _text(option.get("evidence")):
            issues.append(f"{FILE}: {name} option {option['key']} needs a label and evidence")
        else:
            issues += _pointer_problems(option["evidence"], roots)
    if len(set(keys)) != len(keys):
        issues.append(f"{FILE}: {name} repeats an option key")
    if pick.get("recommended") not in keys:
        issues.append(f"{FILE}: {name} recommended is not an option key")
    if pick.get("reversible", "cheap") not in _REVERSIBLE:
        issues.append(f"{FILE}: {name} reversible must be cheap or costly")
    if "affects" in pick and not _text(pick["affects"]):
        issues.append(f"{FILE}: {name} affects must be text")
    return issues


def _unique(ident: str, seen: set[str]) -> list[str]:
    if ident in seen:
        return [f"{FILE}: id {ident} is used twice"]
    seen.add(ident)
    return []


def _pointer_problems(text: str, roots: tuple[Path, ...]) -> list[str]:
    """A local `file:line` in evidence must exist; a URL or other text is not checked."""
    issues = []
    for rel, first, last in pointers(text):
        issues += [f"{FILE}: {p}" for p in pointer_problem(rel, first, last, roots)]
    return issues


def _decided(decided: list, roots: tuple[Path, ...], seen: set[str]) -> list[str]:
    issues = []
    for index, item in enumerate(decided):
        if not isinstance(item, dict) or not _text(item.get("id")):
            issues.append(f"{FILE}: decided {index} needs an id")
            continue
        issues += _unique(item["id"], seen)
        if not all(_text(item.get(key)) for key in ("chosen", "why", "source")):
            issues.append(f"{FILE}: decided {item['id']} needs chosen, why and source")
        else:
            issues += _pointer_problems(item["source"], roots)
        if not isinstance(item.get("deferred", False), bool):
            issues.append(f"{FILE}: decided {item['id']} deferred must be true or false")
    return issues


def _tools(tools: list) -> list[str]:
    if not tools:
        return [f"{FILE}: existing_tools is empty; record what you searched for"]
    issues, searched = [], set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict) or not _text(tool.get("need")):
            issues.append(f"{FILE}: existing_tools {index} needs a `need`")
            continue
        terms = tool.get("searched")
        if not isinstance(terms, list) or not terms or not all(_text(t) for t in terms):
            issues.append(f"{FILE}: existing_tools `{tool['need']}` needs searched terms")
        else:
            searched.update(t.strip() for t in terms)
        issues += _found(tool)
    if not issues and len(searched) < MIN_SEARCHES:
        issues.append(f"{FILE}: record at least {MIN_SEARCHES} different searched terms")
    return issues


def _found(tool: dict) -> list[str]:
    found = tool.get("found")
    if not isinstance(found, list):
        return [f"{FILE}: existing_tools `{tool['need']}` needs a `found` list (may be empty)"]
    issues = []
    for item in found:
        ok = isinstance(item, dict) and all(_text(item.get(k)) for k in ("name", "source", "why"))
        if not ok or item.get("reuse") not in ("yes", "no"):
            issues.append(
                f"{FILE}: a found tool of `{tool['need']}` needs name, source, why, reuse"
            )
    return issues


def writer_choices(workspace: Path, outputs: tuple[str, ...]) -> str | None:
    """The text of the first `Writer choices` section found in the Markdown outputs."""
    for name in outputs:
        path = workspace / name
        if path.suffix != ".md" or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _WRITER.search(text)
        if match is None:
            continue
        level, body = len(match.group(1)), []
        for line in text[match.end() :].splitlines():
            heading = _HEADING.match(line)
            if heading and len(heading.group(1)) <= level:
                break
            body.append(line)
        return "\n".join(body)
    return None


def _writer_choices(workspace: Path, outputs: tuple[str, ...], decided: list) -> list[str]:
    ids = [d["id"] for d in decided if isinstance(d, dict) and _text(d.get("id"))]
    if not ids:
        return []
    section = writer_choices(workspace, outputs)
    if section is None:
        return [f"{FILE}: no output has a `Writer choices` section for the decided ids"]
    return [
        f"{FILE}: decided {i} is not in the `Writer choices` section"
        for i in ids
        if i not in section
    ]


# --- owner defaults ------------------------------------------------------------------------


def defaults_path(root: Path) -> Path:
    return root / "taskwatch" / DEFAULTS_FILE


def defaults_lines(root: Path, job: JobState) -> list[str]:
    """The line in a design job's messages that names `<root>/taskwatch/defaults.json` and its
    short hash."""
    if not is_design(job):
        return []
    path = defaults_path(root)
    try:
        short = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        state = f"sha256 {short}"
    except FileNotFoundError:
        state = "absent"
    except OSError:
        state = "unreadable"
    return [f"Owner defaults: {path} ({state})"]


def _defaults_schema() -> Mapping:
    source = docs_dir("jobs")
    if source is None:
        raise ValueError("job type documents were not found")
    return json.loads((source / DEFAULTS_SCHEMA).read_text(encoding="utf-8"))


def load_defaults(path: Path) -> tuple[list[dict] | None, list[str]]:
    """The validated entries of `defaults.json`, or `([], [])` when the file is absent."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"{DEFAULTS_FILE} cannot be read: {exc}"]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, [f"{DEFAULTS_FILE} is not valid JSON: {exc}"]
    errors = validate(data, _defaults_schema())
    if errors:
        shown = "; ".join(errors[:MAX_ERRORS])
        more = f" (+{len(errors) - MAX_ERRORS} more)" if len(errors) > MAX_ERRORS else ""
        return None, [f"{DEFAULTS_FILE} does not match {DEFAULTS_SCHEMA}: {shown}{more}"]
    seen: set[tuple[str, str]] = set()
    duplicates = []
    for entry in data["defaults"]:
        scope = "global" if entry["scope"] == "global" else str(Path(entry["scope"]).resolve())
        pair = (entry["id"], scope)
        if pair in seen:
            duplicates.append(f"{DEFAULTS_FILE}: duplicate entry for {entry['id']} in {scope}")
        seen.add(pair)
    if duplicates:
        return None, duplicates
    return list(data["defaults"]), []


def resolve_defaults(entries: list[dict], target: Path) -> dict[str, dict]:
    """Applicable entries for `target`, the most specific scope winning per `id`."""
    resolved_target = target.resolve()
    best: dict[str, tuple[tuple[int, int], dict]] = {}
    for entry in entries:
        rank = _scope_rank(entry["scope"], resolved_target)
        if rank is None:
            continue
        ident = entry["id"]
        if ident not in best or rank > best[ident][0]:
            best[ident] = (rank, entry)
    return {ident: pair[1] for ident, pair in best.items()}


def _scope_rank(scope: str, target: Path) -> tuple[int, int] | None:
    if scope == "global":
        return (0, 0)
    path = Path(scope).resolve()
    if target == path or target.is_relative_to(path):
        return (1, len(path.parts))
    return None


def _default_problems(path: Path, target: Path, picks: list, applied: list[str]) -> list[str]:
    entries, issues = load_defaults(path)
    if entries is None:
        return issues
    known = {entry["id"] for entry in entries}
    applicable = resolve_defaults(entries, target)
    for ident in applied:
        if ident not in known:
            issues.append(f"{FILE}: defaults_applied {ident} is not in {DEFAULTS_FILE}")
    for pick in picks:
        if isinstance(pick, dict) and _text(pick.get("id")) and pick["id"] in applicable:
            win = applicable[pick["id"]]
            issues.append(
                f"{FILE}: pick {pick['id']} is already settled by {DEFAULTS_FILE} "
                f"({win['scope']}: {win['value']!r})"
            )
    return issues


# --- the owner's answer --------------------------------------------------------------------


def parse_picks(source: str) -> dict | None:
    """The last `PICKS:` line of an approval source: `ok` and `<id>=<value>` tokens, else None."""
    lines = _PICKS_LINE.findall(source)
    if not lines:
        return None
    try:
        tokens = shlex.split(lines[-1])
    except ValueError:
        return None
    answers = {}
    for token in tokens:
        if token == "ok":
            continue
        ident, sep, value = token.partition("=")
        if not (sep and ident and value):
            return None
        answers[ident] = value
    return {"ok": "ok" in tokens, "answers": answers}


def answer_for(pick: Mapping, parsed: dict) -> str | None:
    """The owner's answer to a pick: the named key or text, else the default when `ok`."""
    if pick["id"] in parsed["answers"]:
        return parsed["answers"][pick["id"]]
    return pick["recommended"] if parsed["ok"] else None


def pick_count(workspace: Path) -> int:
    """How many picks `choices.json` in the workspace holds; 0 when absent or unreadable."""
    data, _ = load(workspace / FILE)
    picks = data.get("picks") if data else None
    return len(picks) if isinstance(picks, list) else 0


# --- what the brief must list --------------------------------------------------------------


def brief_expectations(
    jobs: Jobs, job: JobState, workspace: Path
) -> tuple[list[str], list[str], list[str]]:
    """(awaiting, listed, problems): pick ids the brief marks `awaiting owner`, ids it must list
    in `Choices made`, and reasons the owner's picks were not followed."""
    if is_design(job):
        data, _ = load(workspace / FILE)
        return _ids(data, "picks"), [], []
    if job.review != "code":
        return [], [], []
    records = jobs.load()
    listed, issues = [], []
    for ident in job.predecessors:
        pred = records.get(ident)
        if pred is None or not is_design(pred):
            continue
        data, bad = load(Path(pred.workspace) / FILE)
        issues += [f"predecessor {ident}: {p}" for p in bad]
        picks = _ids(data, "picks")
        listed += [*picks, *_ids(data, "decided")]
        issues += _unanswered(job, ident, data) if picks else []
    return [], listed, issues


def _ids(data: dict | None, key: str) -> list[str]:
    items = data.get(key) if data else None
    if not isinstance(items, list):
        return []
    return [i["id"] for i in items if isinstance(i, dict) and _text(i.get("id"))]


def _unanswered(job: JobState, ident: str, data: dict | None) -> list[str]:
    parsed = parse_picks(job.approval_source)
    if parsed is None:
        return [f"the approval source has no PICKS: line for the picks of job {ident}"]
    return [
        f"no answer to pick {pick['id']} of job {ident} in the PICKS: line"
        for pick in (data or {}).get("picks", [])
        if isinstance(pick, dict) and "id" in pick and answer_for(pick, parsed) is None
    ]


# --- what the owner reads ------------------------------------------------------------------


def render_picks(data: Mapping) -> list[str]:
    """The pick list of the full brief: each pick with its options and why."""
    lines = []
    for number, pick in enumerate(data.get("picks", []), 1):
        labels = {o["key"]: o["label"] for o in pick["options"]}
        rec = pick["recommended"]
        lines.append(f"{number}. {pick['id']}: {pick['question']} (recommend {rec}: {labels[rec]})")
        lines += [f"   {o['key']}) {o['label']} [{o['evidence']}]" for o in pick["options"]]
        lines.append(f"   why: {pick['why']}")
    return lines

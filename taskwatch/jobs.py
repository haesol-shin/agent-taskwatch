"""Durable job snapshots sharing the existing state lock and JSONL publication."""

import hashlib
import json
import math
import re
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType

from .job_env import validate_env
from .job_legacy import upgrade
from .job_store import Store, read_snapshot
from .state import RecordError, State, _create_new, _make_dirs

STAGES = frozenset(
    {
        "queued",
        "work",
        "check",
        "review",
        "fix",
        "approval_wait",
        "deliver_pending",
        "done",
        "user_wait",
        "cancelled",
    }
)
REVIEWS = frozenset({"code", "design", "source"})
# Records written before the field was renamed used `kind` with four values.
_OLD_KINDS = {"code": "code", "design": "design", "document": "source", "research": "source"}
MIN_REVIEWS = 1
MAX_REVIEWS = 3
DEFAULT_MAX_REVIEWS = 3
MAX_RECOVERIES = 3

DEFAULT_BRANCH_PREFIX = "taskwatch/"
_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(/[A-Za-z0-9][A-Za-z0-9_-]*)*[/-]?")


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value):
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class JobState:
    id: str
    receipt_key: str
    approval_source: str
    approval_scope: str
    prompt: str
    review: str
    target: str
    intent: str
    spec: str
    outputs: tuple[str, ...]
    check_command: tuple[str, ...]
    checklist: str
    progress: str
    inputs: Mapping[str, tuple[str, ...]]
    input_hashes: Mapping[str, str]
    predecessors: tuple[str, ...]
    expected_wait_seconds: int
    approval_required: bool
    stage: str = "queued"
    wait_reason: str = ""
    attempt: int = 0
    unit: str = ""
    boot_id: str = ""
    session_path: str = ""
    session_dir: str = ""
    workspace: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    last_progress_at: float = 0.0
    progress_hash: str = ""
    evidence_at: float = 0.0
    stall_suspected: bool = False
    recoveries: int = 0
    # Review budget. `reviews_used` counts the attempts of the current cycle, the first review
    # included; each is reserved before it starts. `review_cycle_start` indexes `reviews`, and
    # `review_inflight` is the attempt holding a reservation whose review has not completed.
    max_reviews: int = DEFAULT_MAX_REVIEWS
    reviews_used: int = 0
    review_cycle_start: int = 0
    review_inflight: int = 0
    # A terminal HTTP 429 parks the saved stage until this epoch second; 0 is no deadline.
    retry_not_before: float = 0.0
    resume_stage: str = "work"
    events: tuple[Mapping, ...] = ()
    checks: tuple[Mapping, ...] = ()
    reviews: tuple[Mapping, ...] = ()
    fingerprint: str = ""
    basis: str = ""
    error: str = ""
    guideline_hashes: Mapping[str, str] | None = None
    deliveries: tuple[Mapping, ...] = ()
    notices: tuple[Mapping, ...] = ()
    # Set for a review-only job: {"files": [...]} relative to target, or {"commit": full sha}.
    review_target: Mapping | None = None
    # Code jobs. `base` is the optional starting commit or branch at submit and the commit
    # actually used once the work folder exists. The states are empty on older records.
    base: str = ""
    base_note: str = ""
    branch: str = ""
    workspace_state: str = ""
    branch_state: str = ""
    # Code jobs. Pull request units whose commit messages, title and body the runner checks:
    # [{"id", "base", "head"}]. Their text files live in the job folder under pr/<id>/.
    public_text: tuple[Mapping, ...] = ()
    # Per unit: the shas of base, head and tree and the text fingerprint the last check saw.
    texts: Mapping[str, Mapping] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, (Mapping, list, tuple)):
                object.__setattr__(self, item.name, _freeze(value))

    def to_json(self) -> dict:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class JobConfig:
    slots: int = 2
    stall_seconds: int = 1800
    omp_command: tuple[str, ...] = ("omp",)
    rpc_timeout: int = 3600
    deliver_command: tuple[str, ...] = ("false", "{message}")
    review_command: tuple[str, ...] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    bullet_one_line: bool = False
    language: str | None = None  # None: the card is written in the language of the intent
    review_mode: str = "rpc"  # "print": the review command runs once per review, not over RPC
    max_reviews: int = DEFAULT_MAX_REVIEWS  # reviews per cycle, the first included


COMMIT_RULES = ("bullet_one_line",)
LANGUAGES = ("en", "ko")  # the keys of `brief.LABELS`
LANGUAGE_NAMES = {"en": "English", "ko": "Korean"}
REVIEW_MODES = ("rpc", "print")


def _review_mode(obj: dict) -> str:
    mode = obj.get("review_mode", "rpc")
    if mode not in REVIEW_MODES:
        raise ValueError(f"review_mode must be one of: {', '.join(REVIEW_MODES)}")
    if mode == "print" and "review_command" not in obj:
        raise ValueError("review_mode print needs review_command")
    return mode


def _max_reviews(value: object) -> int:
    if type(value) is not int or not MIN_REVIEWS <= value <= MAX_REVIEWS:
        raise ValueError(f"max_reviews must be an integer from {MIN_REVIEWS} to {MAX_REVIEWS}")
    return value


def load_job_config(root: Path) -> JobConfig:
    path = root / "taskwatch" / "jobs-config.json"
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or set(obj) - {
        "review_command",
        "env",
        "branch_prefix",
        "commit_rules",
        "language",
        "review_mode",
        "max_reviews",
    } != {
        "version",
        "slots",
        "stall_seconds",
        "omp_command",
        "rpc_timeout",
        "deliver_command",
    }:
        raise ValueError(
            "jobs config needs version, slots, stall_seconds, omp_command, rpc_timeout, "
            "deliver_command"
        )
    if type(obj["version"]) is not int or obj["version"] != 1:
        raise ValueError("unknown jobs config version")
    for key in ("slots", "stall_seconds", "rpc_timeout"):
        if type(obj[key]) is not int or obj[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    _strings(obj["omp_command"], "omp_command", nonempty=True)
    if "review_command" in obj:
        _strings(obj["review_command"], "review_command", nonempty=True)
    env = validate_env(obj["env"]) if "env" in obj else {}
    prefix = obj.get("branch_prefix", DEFAULT_BRANCH_PREFIX)
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise ValueError("branch_prefix must be letters, digits, '_' and '-' parts joined by '/'")
    rules = obj.get("commit_rules", {})
    if not isinstance(rules, dict) or set(rules) - set(COMMIT_RULES):
        raise ValueError(
            f"commit_rules is an object with only these keys: {', '.join(COMMIT_RULES)}"
        )
    if any(type(value) is not bool for value in rules.values()):
        raise ValueError("commit_rules values must be true or false")
    language = obj.get("language")
    if "language" in obj and language not in LANGUAGES:
        raise ValueError(f"language must be one of: {', '.join(LANGUAGES)}")
    review_mode = _review_mode(obj)
    max_reviews = _max_reviews(obj["max_reviews"]) if "max_reviews" in obj else DEFAULT_MAX_REVIEWS
    _strings(obj["deliver_command"], "deliver_command", nonempty=True)
    command = obj["deliver_command"]
    if command.count("{message}") != 1 or any(
        "{message}" in part and part != "{message}" for part in command
    ):
        raise ValueError("deliver_command needs exactly one whole-argument {message}")
    return JobConfig(
        obj["slots"],
        obj["stall_seconds"],
        tuple(obj["omp_command"]),
        obj["rpc_timeout"],
        tuple(command),
        tuple(obj["review_command"]) if "review_command" in obj else None,
        env,
        prefix,
        rules.get("bullet_one_line", False),
        language,
        review_mode,
        max_reviews,
    )


def _digest(obj: dict) -> str:
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


class Jobs:
    def __init__(self, root: Path) -> None:
        self.state = State(root.resolve())
        self._store = Store(self.state.dir)

    def folder(self, job: JobState) -> Path:
        return self.state.dir / "jobs" / job.id

    def load(self) -> dict[str, JobState]:
        with self.state.lock():
            index = self._store.prepare(time.time())
            return {
                ident: decode_job(read_snapshot(self.state.dir, ident, receipt)["job"])
                for ident, receipt in index["receipts"].items()
            }

    def save(self, job: JobState, now: float) -> None:
        with self.state.lock():
            self._store.save(job.to_json(), now)

    def submit(self, payload: dict, now: float | None = None) -> JobState:
        now = time.time() if now is None else now
        if not isinstance(payload, dict) or not isinstance(payload.get("receipt_key"), str):
            raise ValueError("submit needs a receipt_key")
        with self.state.lock():
            existing = self.load()
            for job in existing.values():
                if job.receipt_key == payload["receipt_key"]:
                    return job
            data = _submission(payload)
            host = _host_config(self.state.root)
            data.setdefault("max_reviews", host.max_reviews)
            _resolve_receipt_tokens(data, existing, host.branch_prefix)
            data.update(id=uuid.uuid4().hex, last_progress_at=now)
            self._copy_inputs(data)
            job = JobState(**data)
            self.save(job, now)
            return job

    def _copy_inputs(self, job: dict) -> None:
        directory = self.state.dir / "jobs" / job["id"] / "inputs"
        _make_dirs(directory)
        hashes = {}
        for role, sources in {
            "intent": [job["intent"]],
            "spec": [job["spec"]],
            **job["inputs"],
        }.items():
            copied = []
            for index, source in enumerate(sources):
                data = Path(source).read_bytes()
                dest = directory / f"{role}-{index}.txt"
                _create_new(dest, data, 0o600)
                hashes[str(dest)] = hashlib.sha256(data).hexdigest()
                copied.append(str(dest))
            if role in {"intent", "spec"}:
                job[role] = copied[0]
            else:
                job["inputs"][role] = copied
        job["input_hashes"] = hashes

    def verify_inputs(self, job: JobState) -> None:
        for name, digest in job.input_hashes.items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                raise ValueError("copied input does not match its hash")


def _strings(value: object, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{label} must be {'a nonempty' if nonempty else 'a'} list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} needs nonempty strings")


def _relative(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"{label} must be a relative path")
    if ".." in Path(value).parts or value == ".":
        raise ValueError(f"{label} must stay in the workspace")


_COMMON_FIELDS = {
    "receipt_key",
    "approval_source",
    "approval_scope",
    "prompt",
    "review",
    "target",
    "intent",
    "spec",
}
_WORK_FIELDS = {"outputs", "check_command", "checklist", "progress"}
_OPTIONAL_FIELDS = {
    "inputs",
    "predecessors",
    "expected_wait_seconds",
    "approval_required",
    "base",
    "max_reviews",
    "public_text",
}
UNIT_FILES = ("title.txt", "body.md")
_UNIT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# The headings of docs/spec-template.md. A code or design job's spec answers each one, or says
# `n/a: <reason>`; a source job and a review-only job have no design to question.
SPEC_REVIEWS = frozenset({"code", "design"})
SPEC_HEADINGS = (
    "Purpose and cause",
    "Options and common practice",
    "Homes",
    "Overlaps",
    "Stable text",
    "Needed now",
    "Repeated judgments",
    "Owner decisions and asks",
    "Other users",
    "Writer choices",
    "Writes outside the workspace",
    "Units",
    "Public text",
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_LINE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def spec_sections(text: str) -> dict[str, str]:
    """The `## ` sections of a spec, comments removed. A repeated heading keeps its first body."""
    parts = _HEADING_LINE.split(_COMMENT.sub("", text))
    sections: dict[str, str] = {}
    for name, body in zip(parts[1::2], parts[2::2], strict=True):
        sections.setdefault(name, body.strip())
    return sections


def spec_problems(text: str) -> list[str]:
    sections = spec_sections(text)
    problems = []
    for name in SPEC_HEADINGS:
        body = sections.get(name)
        if body is None:
            problems.append(f"missing heading: {name}")
        elif not body:
            problems.append(f"empty heading: {name}")
        elif re.match(r"n/a(?!\w)", body, re.IGNORECASE) and not re.match(
            r"n/a:\s*\S", body, re.IGNORECASE
        ):
            problems.append(f"n/a needs a reason, `n/a: <reason>`: {name}")
    return problems


def _spec_template(spec: str) -> None:
    problems = spec_problems(Path(spec).read_text(encoding="utf-8"))
    if problems:
        raise ValueError(f"the spec does not follow docs/spec-template.md: {'; '.join(problems)}")


def _submission(payload: dict) -> dict:
    # A review target replaces the work contract: there is no work stage to describe.
    review_only = isinstance(payload, dict) and "review_target" in payload
    if isinstance(payload, dict) and "kind" in payload:
        raise ValueError(
            "`kind` is not accepted; name the field `review` (code, design, or source)"
        )
    required = _COMMON_FIELDS | ({"review_target"} if review_only else _WORK_FIELDS)
    if required - payload.keys() or payload.keys() - required - _OPTIONAL_FIELDS:
        raise ValueError("submit has missing or unknown fields")
    data = {**payload}
    for key in required - {"outputs", "check_command", "review_target"}:
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    if data["review"] not in REVIEWS:
        raise ValueError("unknown review; use code, design, or source")
    data["target"] = str(Path(data["target"]).resolve(strict=True))
    if not Path(data["target"]).is_dir():
        raise ValueError("target must be a directory")
    if review_only:
        _review_fields(data)
    else:
        _work_fields(data)
        if data["review"] in SPEC_REVIEWS:
            _spec_template(data["spec"])
    if "base" in data:
        _base_field(data, review_only)
    if "public_text" in data:
        _public_text_field(data, review_only)
    data.setdefault("predecessors", [])
    _strings(data["predecessors"], "predecessors")
    _controls(data)
    inputs = data.get("inputs", {})
    if not isinstance(inputs, dict) or inputs.keys() - {"work", "review"}:
        raise ValueError("inputs must map work/review to file lists")
    for role, paths in inputs.items():
        _strings(paths, role)
    data["inputs"] = {role: list(paths) for role, paths in inputs.items()}
    return data


def _controls(data: dict) -> None:
    data.setdefault("expected_wait_seconds", 0)
    data.setdefault("approval_required", False)
    if type(data["expected_wait_seconds"]) is not int or data["expected_wait_seconds"] < 0:
        raise ValueError("expected_wait_seconds must be a nonnegative integer")
    if type(data["approval_required"]) is not bool:
        raise ValueError("approval_required must be boolean")
    if "max_reviews" in data:
        _max_reviews(data["max_reviews"])


def _base_field(data: dict, review_only: bool) -> None:
    base = data["base"]
    if not isinstance(base, str) or not base.strip() or base.startswith("-"):
        raise ValueError("base must be a commit or branch name")
    if data["review"] != "code" or review_only:
        raise ValueError("base applies only to code jobs that build a result")


def _host_config(root: Path) -> JobConfig:
    """The host settings a submission freezes; the built-in defaults before `init`."""
    try:
        return load_job_config(root)
    except FileNotFoundError:
        return JobConfig()


def _resolve_receipt_tokens(data: dict, existing: Mapping[str, JobState], prefix: str) -> None:
    """Resolve receipt keys in `predecessors` to job ids and `@<receipt key>` in `base` to a
    branch name, so a chain can be submitted before its jobs have ids."""
    by_receipt = {job.receipt_key: job for job in existing.values()}
    predecessors = []
    missing = []
    for token in data["predecessors"]:
        if token in existing:
            predecessors.append(token)
        elif token in by_receipt:
            predecessors.append(by_receipt[token].id)
        else:
            missing.append(token)
    if missing:
        raise ValueError(f"unknown predecessors: {', '.join(sorted(set(missing)))}")
    data["predecessors"] = predecessors
    base = data.get("base", "")
    if base.startswith("@"):
        key = base[1:]
        pred = by_receipt.get(key)
        if not key or pred is None:
            raise ValueError(f"unknown receipt key in base: {base}")
        if pred.review != "code" or pred.review_target is not None:
            raise ValueError(f"base {base} is not a code job that builds a branch")
        data["base"] = pred.branch or f"{prefix}{pred.id[:8]}"


def _ref(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise ValueError(f"{label} must be a commit or branch name")
    return value


def _public_text_field(data: dict, review_only: bool) -> None:
    """Normalize the pull request units; a stacked unit's base may be another unit's head."""
    units = data["public_text"]
    if data["review"] != "code" or review_only:
        raise ValueError("public_text applies only to code jobs that build a result")
    if not isinstance(units, list) or not units:
        raise ValueError("public_text must be a nonempty list of units")
    normal = []
    for unit in units:
        if not isinstance(unit, dict) or unit.keys() - {"id", "base", "head"} or "base" not in unit:
            raise ValueError("a public_text unit needs id and base, and may have head")
        if not isinstance(unit.get("id"), str) or _UNIT_ID.fullmatch(unit["id"]) is None:
            raise ValueError("a public_text id is lowercase letters, digits and hyphens")
        normal.append(
            {
                "id": unit["id"],
                "base": _ref(unit["base"], "public_text base"),
                "head": _ref(unit.get("head", "HEAD"), "public_text head"),
            }
        )
    if len({unit["id"] for unit in normal}) != len(normal):
        raise ValueError("public_text ids must be unique")
    # A head may name a commit the job has not made yet, so only an external base is resolved
    # now, and pinned: a later move of that ref must not change the reviewed range. A base that
    # is another unit's head stays symbolic.
    heads = {unit["head"] for unit in normal}
    for unit in normal:
        if unit["base"] not in heads:
            unit["base"] = _resolve_commit(Path(data["target"]), unit["base"], "public_text base")
    data["public_text"] = normal


def _work_fields(data: dict) -> None:
    for key in ("outputs", "check_command"):
        _strings(data[key], key, nonempty=True)
    for item in data["outputs"]:
        _relative(item, "output")
    for key in ("checklist", "progress"):
        _relative(data[key], key)


def _review_fields(data: dict) -> None:
    """Fill the work contract with what a review-only job has: nothing to build or check."""
    target = data["review_target"]
    if not isinstance(target, dict) or len(target) != 1 or target.keys() - {"files", "commit"}:
        raise ValueError("review_target needs exactly one of files or commit")
    root = Path(data["target"])
    if "files" in target:
        _strings(target["files"], "review_target files", nonempty=True)
        for item in target["files"]:
            _relative(item, "review_target file")
            if not (root / item).is_file():
                raise ValueError(f"review_target file does not exist: {item}")
        data["review_target"] = {"files": list(target["files"])}
        data["outputs"] = list(target["files"])
    else:
        data["review_target"] = {"commit": _resolve_commit(root, target["commit"])}
        data["outputs"] = []
    data.update(check_command=[], checklist="", progress="", resume_stage="review")


def _resolve_commit(root: Path, revision: object, label: str = "review_target commit") -> str:
    if not isinstance(revision, str) or not revision.strip() or revision.startswith("-"):
        raise ValueError(f"{label} must be a revision")
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError(f"{label} is not a commit of the target: {revision}")
    return sha


def _upgrade_record(job: dict) -> dict:
    """Read a record that still has the old `kind` field as its `review` value."""
    if "kind" not in job or "review" in job:
        return job
    upgraded = {key: value for key, value in job.items() if key != "kind"}
    upgraded["review"] = _OLD_KINDS.get(job["kind"])
    return upgraded


def decode_job(data: dict) -> JobState:
    """A validated record as a JobState; an older record is migrated, never rewritten."""
    _validate_record(data)
    return JobState(**upgrade(data, DEFAULT_MAX_REVIEWS))


def _validate_record(job: dict) -> None:
    if not isinstance(job.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", job["id"]):
        raise RecordError("jobs.jsonl: invalid job id")
    if job.get("stage") not in STAGES or job.get("review") not in REVIEWS:
        raise RecordError("jobs.jsonl: unknown stage or review")
    for key in ("attempt", "recoveries"):
        if type(job.get(key)) is not int or job[key] < 0:
            raise RecordError(f"jobs.jsonl: invalid {key}")
    review_only = job.get("review_target") is not None
    # A review-only job has no work contract; its checklist and progress stay empty.
    texts = (
        "receipt_key",
        "approval_source",
        "approval_scope",
        "prompt",
        "target",
        "intent",
        "spec",
    )
    for key in texts if review_only else (*texts, "checklist", "progress"):
        if not isinstance(job.get(key), str) or not job[key]:
            raise RecordError(f"jobs.jsonl: missing {key}")
    for key in ("outputs", "check_command", "predecessors", "events", "checks", "reviews"):
        if not isinstance(job.get(key), list):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    if not isinstance(job.get("input_hashes"), dict):
        raise RecordError("jobs.jsonl: missing input hashes")
    for key in ("started_at", "ended_at", "last_progress_at", "evidence_at"):
        if type(job.get(key)) not in {int, float} or not math.isfinite(job[key]):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    _validate_controls(job)
    _validate_budget(job)
    if job.get("review_target") is not None:
        _validate_review_target(job)
    _validate_manifest(job)
    for key in ("deliveries", "notices"):
        attempts = job.get(key, [])
        if not isinstance(attempts, list) or any(
            not isinstance(row, dict) or row.get("status") not in {"started", "success", "unknown"}
            for row in attempts
        ):
            raise RecordError(f"jobs.jsonl: invalid {key} attempts")
    _validate_public_text(job)


def _validate_public_text(job: dict) -> None:
    units = job.get("public_text", [])
    if not isinstance(job.get("texts", {}), dict):
        raise RecordError("jobs.jsonl: invalid texts")
    if not isinstance(units, list) or any(
        not isinstance(unit, dict)
        or unit.keys() != {"id", "base", "head"}
        or any(not isinstance(value, str) or not value for value in unit.values())
        for unit in units
    ):
        raise RecordError("jobs.jsonl: invalid public_text")


def _validate_review_target(job: dict) -> None:
    target = job["review_target"]
    if not isinstance(target, dict) or len(target) != 1:
        raise RecordError("jobs.jsonl: invalid review target")
    if "commit" in target:
        valid = isinstance(target["commit"], str) and re.fullmatch(
            r"[0-9a-f]{40}", target["commit"]
        )
    else:
        valid = target.get("files") == job["outputs"] and bool(job["outputs"])
    if not valid:
        raise RecordError("jobs.jsonl: invalid review target")


def _validate_budget(job: dict) -> None:
    """The review budget: the older `fix_rounds`, or the current fields, never both."""
    if "reviews_used" not in job:
        if type(job.get("fix_rounds", 0)) is not int or job.get("fix_rounds", 0) < 0:
            raise RecordError("jobs.jsonl: invalid fix_rounds")
        return
    if "fix_rounds" in job:
        raise RecordError("jobs.jsonl: fix_rounds beside reviews_used")
    maximum = job.get("max_reviews", DEFAULT_MAX_REVIEWS)
    if type(maximum) is not int or not MIN_REVIEWS <= maximum <= MAX_REVIEWS:
        raise RecordError("jobs.jsonl: invalid max_reviews")
    # An overcount is conservative, so `reviews_used` has no upper bound here.
    counts = {
        "reviews_used": None,
        "review_cycle_start": len(job["reviews"]),
        "review_inflight": None,
    }
    for key, limit in counts.items():
        value = job.get(key, 0)
        if type(value) is not int or value < 0 or (limit is not None and value > limit):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    deadline = job.get("retry_not_before", 0.0)
    if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline < 0:
        raise RecordError("jobs.jsonl: invalid retry_not_before")


def _validate_controls(job: dict) -> None:
    for key in ("approval_required", "stall_suspected"):
        if type(job.get(key)) is not bool:
            raise RecordError(f"jobs.jsonl: invalid {key}")
    for key in (
        "wait_reason",
        "unit",
        "boot_id",
        "session_path",
        "session_dir",
        "workspace",
        "progress_hash",
        "fingerprint",
        "basis",
        "error",
        "resume_stage",
    ):
        if not isinstance(job.get(key), str):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    for key in ("base", "base_note", "branch", "workspace_state", "branch_state"):
        if not isinstance(job.get(key, ""), str):
            raise RecordError(f"jobs.jsonl: invalid {key}")
    if job["resume_stage"] not in STAGES:
        raise RecordError("jobs.jsonl: invalid resume stage")
    if type(job.get("expected_wait_seconds")) is not int or job["expected_wait_seconds"] < 0:
        raise RecordError("jobs.jsonl: invalid expected wait")
    review_only = job.get("review_target") is not None
    try:
        for key in ("outputs", "check_command", "predecessors"):
            _strings(job[key], key, nonempty=key == "outputs" and not review_only)
        for relative in [
            *job["outputs"],
            *([] if review_only else [job["checklist"], job["progress"]]),
        ]:
            _relative(relative, "job file")
    except ValueError as exc:
        raise RecordError(f"jobs.jsonl: {exc}") from exc


def _validate_manifest(job: dict) -> None:
    inputs = job.get("inputs")
    if not isinstance(inputs, dict) or inputs.keys() - {"work", "review"}:
        raise RecordError("jobs.jsonl: invalid role inputs")
    expected = {job["intent"], job["spec"]}
    for role, paths in inputs.items():
        try:
            _strings(paths, role)
        except ValueError as exc:
            raise RecordError(f"jobs.jsonl: {exc}") from exc
        expected.update(paths)
    hashes = job["input_hashes"]
    if hashes.keys() != expected or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in hashes.values()
    ):
        raise RecordError("jobs.jsonl: incomplete input hash manifest")


def progress_fingerprint(job: JobState, workspace: Path, folder: Path) -> str:
    """Hash progress and outputs, never growing session or command logs.

    The progress record lives in the job folder; outputs live in the workspace.
    """
    digest = hashlib.sha256()
    for base, relative in [(folder, job.progress), *((workspace, item) for item in job.outputs)]:
        base = base.resolve()
        path = (base / relative).resolve()
        inside = not Path(relative).is_absolute() and path.is_relative_to(base)
        data = path.read_bytes() if inside and path.is_file() else b""
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()

"""Terminal HTTP 429 failures and the retry deadline they evidence.

Only a terminal diagnostic counts: the error of a finished prompt, or the stderr of a review
command that exited with a failure. Conversation text and tool output are never scanned. A
hint is an RFC 9110 `Retry-After` (integer seconds or an HTTP date) or the provider's integer
`retry-after-ms`; the millisecond form is not an RFC fractional second. Nothing here sleeps or
invents a delay.
"""

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

_DAYS = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_LONG_DAYS = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_DATE = (
    rf"(?:{_DAYS}, [0-9]{{2}} {_MONTHS} [0-9]{{4}} [0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}} GMT"
    rf"|{_LONG_DAYS}, [0-9]{{2}}-{_MONTHS}-[0-9]{{2}} [0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}} GMT"
    rf"|{_DAYS} {_MONTHS} (?: [0-9]|[0-9]{{2}}) [0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}} [0-9]{{4}})"
)
_MILLISECONDS = re.compile(
    r"\bretry-after-ms[\"']?\s*[=:]\s*[\"']?([0-9]+)(?=$|[\s\"',;}\]])", re.IGNORECASE
)
_RETRY_AFTER = re.compile(
    rf"\bretry-after[\"']?\s*[=:]\s*[\"']?([0-9]+|{_DATE})(?=$|[\s\"',;}}\]])",
    re.IGNORECASE,
)
# The error of a finished prompt: `prompt ended error: 429 ...`, or the bare detail.
_PROMPT_429 = re.compile(r"\A\s*(?:prompt ended (?:error|failed):\s*)?429\b", re.IGNORECASE)
# A review command that is not an RPC session says it in its own words on standard error.
_PRINT_429 = re.compile(
    r"\b(?:HTTP(?:/\d(?:\.\d)?)?[ :]+429|status(?: code)?[ :=]+429|429 Too Many Requests)\b",
    re.IGNORECASE,
)
_HINT_LINE = re.compile(r"^retry-after(?:-ms)?[\"']?\s*[=:]", re.IGNORECASE)
_FINAL_ERROR = re.compile(r"\b(?:final|terminal)\s+(?:error|failure|failed)\s*[:=]", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)(bearer\s+|authorization[\"']?\s*[=:]\s*[\"']?|api[_-]?key[\"']?\s*[=:]\s*[\"']?"
    r"|token[\"']?\s*[=:]\s*[\"']?)[A-Za-z0-9._~+/=-]{8,}|\bsk-[A-Za-z0-9_-]{8,}"
)


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A terminal 429. `retry_at` is the latest valid deadline (epoch seconds); None when the
    diagnostic gave no usable hint."""

    retry_at: float | None


def scrub(text: str) -> str:
    """The diagnostic without anything that looks like a credential."""
    return _SECRET.sub(lambda m: f"{m.group(1) or ''}[redacted]", text)


def _deadlines(text: str, now: float) -> list[float]:
    found = [now + float(m.group(1)) / 1000 for m in _MILLISECONDS.finditer(text)]
    for match in _RETRY_AFTER.finditer(text):
        value = match.group(1)
        if value.isdigit():
            found.append(now + float(value))
            continue
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            found.append(when.timestamp())
        except (ValueError, TypeError, OverflowError):
            continue
    return [max(0.0, d) for d in found if math.isfinite(d)]


def _limit(text: str, now: float | None) -> RateLimit:
    deadlines = _deadlines(text, time.time() if now is None else now)
    return RateLimit(max(deadlines) if deadlines else None)


def prompt_rate_limit(message: str, now: float | None = None) -> RateLimit | None:
    """`message` is the error of a finished prompt, with or without its `prompt ended` prefix."""
    return _limit(message, now) if _PROMPT_429.match(message) else None


def print_rate_limit(stderr: str, now: float | None = None) -> RateLimit | None:
    """Read the final HTTP diagnostic block, not a rate limit from an earlier fallback."""
    lines = _diagnostic_lines(stderr)
    for index in range(len(lines) - 1, -1, -1):
        if not _PRINT_429.search(lines[index]):
            continue
        body = "\n".join(
            line for line in lines[index + 1 :] if not _HINT_LINE.match(line.strip())
        ).strip()
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                return None  # A later plain-text failure superseded the HTTP diagnostic.
            if not _rate_limit_body(payload):
                return None
        return _limit("\n".join(lines[index:]), now)
    return None


def _diagnostic_lines(stderr: str) -> list[str]:
    """An explicit final-error marker starts a new diagnostic, even on the same line."""
    decoder = json.JSONDecoder()
    parts = []
    start = index = 0
    while index < len(stderr):
        if stderr[index] in "{[":
            try:
                _, size = decoder.raw_decode(stderr[index:])
            except ValueError:
                pass
            else:
                index += size
                continue  # A marker inside a structured response is not a new diagnostic.
        marker = _FINAL_ERROR.match(stderr, index)
        if marker and index > start:
            parts.append(stderr[start:index].rstrip(" ;"))
            start = index
            index = marker.end()
        else:
            index += 1
    parts.append(stderr[start:])
    return "\n".join(parts).strip().splitlines()


def _rate_limit_body(payload: object) -> bool:
    """A structured error with another status or type is a later failure, not a 429 body."""
    if not isinstance(payload, dict):
        return False
    status = payload.get("status", payload.get("status_code", payload.get("code")))
    if status is not None and status not in (429, "429"):
        return False
    error = payload.get("error")
    kind = error.get("type", error.get("code")) if isinstance(error, dict) else error
    return kind in (None, "rate_limit_error", 429, "429")

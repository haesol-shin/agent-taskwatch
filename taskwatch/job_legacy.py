"""Decode a job record written before review reservations.

An old record has `fix_rounds` and no `reviews_used`. The decode is a pure function of the record,
so every reader derives the same budget; it never rewrites storage and never grants a budget the
record cannot prove. Old fields exist only here: the runtime policy has no second counter.
"""

import math

from .rate_limit import prompt_rate_limit

_WAITING = "review_exhausted"
_UNPROVEN = (
    "the review budget of this older record cannot be proven from its history; "
    "resume it to open a new review cycle"
)


def upgrade(data: dict, default_max: int) -> dict:
    """The record in the current shape; a record that already has `reviews_used` is unchanged."""
    if "reviews_used" in data:
        return data
    job = {key: value for key, value in data.items() if key != "fix_rounds"}
    maximum = job.setdefault("max_reviews", default_max)
    cycle = _cycle(data)
    if cycle is not None:
        used, start, inflight = cycle
        job.update(reviews_used=min(used, maximum), review_cycle_start=start)
        job["review_inflight"] = inflight
        return job
    # Not provable: the cycle counts as used up before another model call.
    job.update(reviews_used=maximum, review_cycle_start=len(data["reviews"]), review_inflight=0)
    if data["stage"] in {"fix", "review"}:
        job.update(
            stage="user_wait",
            wait_reason=_WAITING,
            resume_stage=data["stage"],
            error=_UNPROVEN,
        )
    return job


def _number(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _cycle(data: dict) -> tuple[int, int, int] | None:
    """(reviews used, first review of the cycle, attempt holding a reservation), or None when the
    events and review records do not prove a complete cycle since the latest explicit resume."""
    reviews, events = data["reviews"], data["events"]
    if any(not isinstance(review, dict) for review in reviews):
        return None
    cut = _last_resume(events)
    launches = _launches(events, cut)
    if launches is None:
        return None
    start = 0
    if cut >= 0:
        when = events[cut].get("at")
        if not _number(when) or any(not _number(review.get("at")) for review in reviews):
            return None
        start = sum(review["at"] < when for review in reviews)
    if not launches:
        managed_only = all(
            event.get("type") in {"wait", "approve"}
            or (
                event.get("source") == "scan"
                and event.get("kind") in {"approval_wait", "unit_released"}
            )
            for event in events[cut + 1 :]
        )
        never_started = data["attempt"] == 0 and not reviews and not data.get("unit")
        fresh_resume = cut >= 0 and not reviews[start:]
        unused = (
            data["stage"] in {"queued", "approval_wait"}
            and data.get("fix_rounds", 0) == 0
            and managed_only
            and (never_started or fresh_resume)
        )
        if not unused:
            return None
    used = sum(not excluded for excluded in launches.values())
    if not _correlated(events[cut + 1 :], reviews[start:], launches):
        return None
    running = data["stage"] == "review" and bool(data.get("unit")) and data["ended_at"] <= 0
    if running and (data["attempt"] not in launches or _completed(events, cut, data["attempt"])):
        return None
    # The stored fix rounds are a lower bound on what the cycle spent, never the count itself.
    return max(used, data.get("fix_rounds", 0)), start, data["attempt"] if running else 0


def _last_resume(events: list) -> int:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if isinstance(event, dict) and event.get("type") == "resume":
            return index
    return -1


def _launches(events: list, cut: int) -> dict[int, bool] | None:
    """Attempt -> True when that review launch ended in a terminal 429, which spent no review.
    None when an event cannot be read."""
    launches: dict[int, bool] = {}
    current = None
    for event in events[cut + 1 :]:
        if not isinstance(event, dict):
            return None
        if event.get("source") == "scan" and event.get("kind") == "launched":
            attempt = event.get("attempt")
            current = None
            if event.get("stage") == "review":
                if type(attempt) is not int:
                    return None
                launches[attempt] = False
                current = attempt
        elif event.get("type") == "review":
            current = None
        elif event.get("type") == "step_failed" and current is not None:
            error = event.get("error")
            if isinstance(error, str) and prompt_rate_limit(error) is not None:
                launches[current] = True
    return launches


def _completed(events: list, cut: int, attempt: int) -> bool:
    current = None
    for event in events[cut + 1 :]:
        if event.get("source") == "scan" and event.get("kind") == "launched":
            current = event.get("attempt")
        elif event.get("type") == "review" and current == attempt:
            return True
    return False


def _correlated(events: list, reviews: list, launches: dict[int, bool]) -> bool:
    """Each completion must fit one distinct launch's time interval and persisted review."""
    current, launched = None, None
    completed: set[int] = set()
    index = 0
    for event in events:
        if event.get("source") == "scan" and event.get("kind") == "launched":
            current = event["attempt"] if event.get("stage") == "review" else None
            launched = event.get("at")
            if current is not None and not _number(launched):
                return False
        elif event.get("type") == "review":
            if (
                current is None
                or current in completed
                or launches[current]
                or index >= len(reviews)
            ):
                return False
            reviewed, ended = reviews[index].get("at"), event.get("at")
            if not _number(reviewed) or not _number(ended) or not launched <= reviewed <= ended:
                return False
            completed.add(current)
            index += 1
            current = None
    return index == len(reviews)

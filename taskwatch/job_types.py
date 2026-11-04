"""The job type label of a job, derived from fields that exist. There is no job type field."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import JobState

_LABELS = {"source": "research", "design": "design", "code": "implement"}
CHOICES_FILE = "choices.json"


def builds_design(review: str, review_target: object = None) -> bool:
    """Only work-building design jobs need the design output contract."""
    return review == "design" and review_target is None


def declares_choices(outputs: Iterable[str]) -> bool:
    """Whether the outputs name the root choices file, however the path is spelled."""
    return any(Path(output) == Path(CHOICES_FILE) for output in outputs)


def job_type_label(job: JobState) -> str:
    """research, design, implement, or review (a review-only job has a `review_target`)."""
    return "review" if job.review_target is not None else _LABELS[job.review]

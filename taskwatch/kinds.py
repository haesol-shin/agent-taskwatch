"""The kind label of a job, derived from fields that exist. There is no `kind` field."""

from .jobs import JobState

_LABELS = {"source": "research", "design": "design", "code": "implement"}


def kind_label(job: JobState) -> str:
    """research, design, implement, or review (a review-only job has a `review_target`)."""
    return "review" if job.review_target is not None else _LABELS[job.review]

"""The review basis."""

from collections.abc import Sequence
from pathlib import Path

from .fingerprint import basis_fingerprint


def basis_of(intent: Path, spec: Path, documents: Sequence[tuple[str, Path]]) -> str:
    """The review basis. The reviewer and the gate must name the files the same way, so each
    copied document brings its own name in the copy."""
    named = [("intent", intent), ("spec", spec), *documents]
    return basis_fingerprint(named)

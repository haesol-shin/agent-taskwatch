"""Where the shared review documents live, and which of them a review reads."""

import sys
from pathlib import Path

PROFILES = {
    "code": "code.md",
    "design": "design.md",
    "source": "source.md",
}
COMMON = ("README.md", "common.md")


SOURCE_OUTPUT = "claims.json"


def profile_names(review: str, outputs: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The documents a review reads. A job whose outputs include the claims file also answers to
    the source rules, whatever its own review kind (a design with research included)."""
    names = (*COMMON, PROFILES[review])
    if SOURCE_OUTPUT in outputs and PROFILES["source"] not in names:
        names = (*names, PROFILES["source"])
    return names


def all_names() -> tuple[str, ...]:
    return (*COMMON, *PROFILES.values())


def docs_dir(folder: str) -> Path | None:
    """The source checkout's docs folder, else the one installed with the package."""
    for base in (
        Path(__file__).resolve().parent.parent / "docs" / folder,
        Path(sys.prefix) / "share" / "agent-taskwatch" / folder,
    ):
        if base.is_dir():
            return base
    return None

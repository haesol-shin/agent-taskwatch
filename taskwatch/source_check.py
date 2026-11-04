"""The deterministic half of source review: do the saved sources and the quoted claims exist.

A research job saves what it read and writes its claims in files this check can parse:
`sources/<id>.txt`, `sources/index.json` and `claims.json` in the work folder, and cites a claim
in the result as `[C1]`. Plain code, standard library, no network, no model. It cannot tell that
the saved text is what a URL served, that a quote supports its claim, or that the sources cover
the question; the reviewer judges support (S-3, S-6) for the pairs this check found present.
The format is in docs/jobs/research.md.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CLAIMS = "claims.json"
INDEX = "sources/index.json"
MIN_QUOTE_WORDS = 8
TYPES = ("fact", "number", "absence", "derived")
KINDS = ("full", "snippet")
_ID = re.compile(r"[A-Za-z0-9_-]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CITE = re.compile(r"\[(C\d+)\]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_SPACE = re.compile(r"\s+")
# Quote marks and dashes that fetch tools and editors swap for one another.
_MARKS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


def normalize(text: str) -> str:
    """Whitespace runs to one space, curly quotes to straight, dash variants to `-`."""
    return _SPACE.sub(" ", text.translate(_MARKS)).strip()


def numbers(text: str) -> set[str]:
    """The numbers of a text, with thousands separators removed: `1,234` is `1234`."""
    return {match.replace(",", "") for match in _NUMBER.findall(text)}


@dataclass(frozen=True)
class Result:
    problems: list[str]
    claims: int = 0
    snippet_only: int = 0
    # (claim id, claim, quote, source id, quote found in the saved source)
    rows: list[tuple[str, str, str, str, bool]] = field(default_factory=list)


def check(workspace: Path, outputs: tuple[str, ...]) -> Result:
    """Check the saved sources and claims in `workspace`; `outputs` names the cited result."""
    claims, problems = _claims(workspace)
    if claims is None:
        return Result(problems)
    sources, source_problems = _sources(workspace)
    problems += source_problems
    problems += _citations(workspace, outputs, claims)
    rows, snippet_only = [], 0
    ids = {c["id"] for c in claims if isinstance(c.get("id"), str)}
    for claim in claims:
        found, issues = _claim(claim, ids, sources, workspace)
        problems += issues
        if _snippet_only(claim, sources):
            snippet_only += 1
        rows.append(
            (
                str(claim.get("id", "?")),
                str(claim.get("claim", "")),
                str(claim.get("quote", "")),
                str(claim.get("source", "")),
                found,
            )
        )
    return Result(problems, len(claims), snippet_only, rows)


def _claims(workspace: Path) -> tuple[list[dict] | None, list[str]]:
    try:
        data = json.loads((workspace / CLAIMS).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"required file is missing: {CLAIMS}"]
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, [f"{CLAIMS} is not valid JSON: {exc}"]
    if not isinstance(data, list) or not all(isinstance(c, dict) for c in data):
        return None, [f"{CLAIMS} must be a list of claim objects"]
    issues = []
    seen: set[str] = set()
    for index, claim in enumerate(data):
        ident = claim.get("id")
        if not isinstance(ident, str) or not _CITE.fullmatch(f"[{ident}]"):
            issues.append(f"{CLAIMS}: claim {index} needs an id like C1")
        elif ident in seen:
            issues.append(f"{CLAIMS}: id {ident} is used twice")
        else:
            seen.add(ident)
    return data, issues


def _sources(workspace: Path) -> tuple[dict[str, dict], list[str]]:
    try:
        index = json.loads((workspace / INDEX).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [f"required file is missing: {INDEX}"]
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{INDEX} is not valid JSON: {exc}"]
    if not isinstance(index, dict):
        return {}, [f"{INDEX} must be an object of source ids"]
    issues = []
    for ident, entry in index.items():
        issues += _source(workspace, ident, entry)
    return {k: v for k, v in index.items() if isinstance(v, dict)}, issues


def _source(workspace: Path, ident: str, entry: object) -> list[str]:
    if not _ID.fullmatch(ident) or not isinstance(entry, dict):
        return [f"{INDEX}: source {ident!r} needs a plain id and an object"]
    name = f"source {ident}"
    issues = []
    for key in ("url", "retrieved"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            issues.append(f"{name} has an empty {key}")
    if isinstance(entry.get("retrieved"), str) and entry["retrieved"].strip():
        if not _DATE.match(entry["retrieved"]):
            issues.append(f"{name} retrieved is not an ISO date")
    if entry.get("kind") not in KINDS:
        issues.append(f"{name} kind must be full or snippet")
    try:
        digest = hashlib.sha256((workspace / "sources" / f"{ident}.txt").read_bytes()).hexdigest()
    except OSError:
        return [*issues, f"{name} has no file sources/{ident}.txt"]
    if digest != entry.get("sha256"):
        issues.append(f"{name} file does not match its sha256 in the index")
    return issues


def _claim(
    claim: dict, ids: set[str], sources: dict[str, dict], workspace: Path
) -> tuple[bool, list[str]]:
    """Whether the quote was found in its source, and why the claim fails."""
    name = f"claim {claim.get('id', '?')}"
    kind = claim.get("type")
    if kind not in TYPES or not _text(claim.get("claim")):
        return False, [f"{name} needs a claim text and a type: {', '.join(TYPES)}"]
    if kind == "absence":
        return _absence_quote(claim, name, sources, workspace)
    if kind == "derived":
        return False, _derived(claim, name, ids)
    quote, source = claim.get("quote"), claim.get("source")
    if not _text(quote) or source not in sources:
        return False, [f"{name} needs a quote and a source that is in {INDEX}"]
    issues = []
    found = _present(workspace, source, quote)
    if not found:
        issues.append(f"{name}: the quote is not in source {source}")
    short = len(quote.split()) < MIN_QUOTE_WORDS
    shown = numbers(claim["claim"])
    if short and not (kind == "number" and shown and shown <= numbers(quote)):
        issues.append(f"{name}: the quote is shorter than {MIN_QUOTE_WORDS} words")
    if not shown <= numbers(quote):
        issues.append(f"{name}: a number of the claim is not in its quote")
    return found, issues


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _absence(claim: dict, name: str) -> list[str]:
    terms = claim.get("searched")
    if not isinstance(terms, list) or not any(_text(t) for t in terms):
        return [f"{name} says nothing was found but lists no searched terms"]
    return []


def _absence_quote(
    claim: dict, name: str, sources: dict[str, dict], workspace: Path
) -> tuple[bool, list[str]]:
    """An absence claim needs search terms; a quote it gives is checked like any other."""
    issues = _absence(claim, name)
    quote = claim.get("quote")
    if not _text(quote):
        return False, issues
    source = claim.get("source")
    if source not in sources:
        return False, [*issues, f"{name} quotes a source that is not in {INDEX}"]
    found = _present(workspace, source, quote)
    if not found:
        issues.append(f"{name}: the quote is not in source {source}")
    if not numbers(claim["claim"]) <= numbers(quote):
        issues.append(f"{name}: a number of the claim is not in its quote")
    return found, issues


def _derived(claim: dict, name: str, ids: set[str]) -> list[str]:
    parents = claim.get("from")
    if not isinstance(parents, list) or not parents:
        return [f"{name} is derived and must name the claims it comes from"]
    bad = [p for p in parents if p not in ids or p == claim.get("id")]
    if bad:
        return [f"{name} derives from {', '.join(map(str, bad))}, which is not another claim"]
    return []


def _present(workspace: Path, source: str, quote: str) -> bool:
    try:
        text = (workspace / "sources" / f"{source}.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return normalize(quote) in normalize(text)


def _snippet_only(claim: dict, sources: dict[str, dict]) -> bool:
    return claim.get("type") in ("fact", "number") and (
        sources.get(claim.get("source"), {}).get("kind") == "snippet"
    )


def _citations(workspace: Path, outputs: tuple[str, ...], claims: list[dict]) -> list[str]:
    cited: set[str] = set()
    for name in outputs:
        path = workspace / name
        if path.suffix == ".md" and path.is_file():
            cited |= set(_CITE.findall(path.read_text(encoding="utf-8", errors="replace")))
    ids = {c["id"] for c in claims if isinstance(c.get("id"), str)}
    issues = [f"the result cites [{c}], which is not in {CLAIMS}" for c in sorted(cited - ids)]
    return issues + [f"claim {c} is cited nowhere in the result" for c in sorted(ids - cited)]


def table(rows: list[tuple[str, str, str, str, bool]]) -> str:
    """What the reviewer reads: each claim with its quote, source and whether the quote exists."""
    lines = ["| claim | statement | quote | source | quote found |", "|---|---|---|---|---|"]
    for ident, claim, quote, source, found in rows:
        cells = [ident, claim, quote, source, "yes" if found else "no"]
        escaped = (c.replace("|", "\\|").replace("\n", " ") for c in cells)
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines) + "\n"

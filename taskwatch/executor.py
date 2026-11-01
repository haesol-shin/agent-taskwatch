"""Read the verdict line and the finding lines of a review output.

A review output holds exactly one line that is a JSON object:
{"verdict": "PASS" or "FAIL", "blocking": true or false,
"checked": "<what you compared>", "not_checked": "<what you did not>"}.
A `reviewer` key is accepted and ignored: the runner knows which command reviewed.

Each finding has a heading `### F<n>` and one JSON line
{"finding": "F<n>", "severity": "P0".."P3", "category": "<C-9 category>",
 "rule": "<rule id>", "location": "<where>", "summary": "<one sentence>"}.
`summary` is optional here; a PASS needs one on every P2 or P3 finding (the runner checks it).
The two sets of ids must match. Only these lines are read, never the free text.
"""

import json
import re

SEVERITIES = ("P0", "P1", "P2", "P3")
MAX_SUMMARY_CHARS = 160
CATEGORIES = ("intent", "evidence", "behavior", "clarity", "other")
_HEADING = re.compile(r"^###\s+(F\d+)\b", re.MULTILINE)


def _json_lines(body: str, key: str) -> list[dict]:
    found = []
    for line in body.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict) and key in obj:
            found.append(obj)
    return found


def _verdict_line(body: str) -> tuple[str, bool]:
    found = _json_lines(body, "verdict")
    if len(found) != 1:
        raise ValueError("the review output needs exactly one verdict line")
    obj = found[0]
    verdict, blocking = obj.get("verdict"), obj.get("blocking")
    if verdict not in {"PASS", "FAIL"} or not isinstance(blocking, bool):
        raise ValueError("the verdict line needs verdict PASS or FAIL and a boolean blocking")
    if verdict == "PASS" and blocking:
        raise ValueError("a PASS cannot carry a blocking finding")
    return verdict, blocking


def _finding_lines(body: str, verdict: str) -> list[dict]:
    """The finding lines, validated against the headings and the verdict."""
    findings = []
    for obj in _json_lines(body, "finding"):
        ident = obj.get("finding")
        if not isinstance(ident, str) or re.fullmatch(r"F\d+", ident) is None:
            raise ValueError("a finding line needs an id like F1")
        if obj.get("severity") not in SEVERITIES or obj.get("category") not in CATEGORIES:
            raise ValueError(f"finding {ident} needs a severity P0 to P3 and a known category")
        for key in ("rule", "location"):
            if not isinstance(obj.get(key), str) or not obj[key].strip():
                raise ValueError(f"finding {ident} needs a nonempty {key}")
        finding = {
            "finding": ident,
            "severity": obj["severity"],
            "category": obj["category"],
            "rule": obj["rule"].strip(),
            "location": obj["location"].strip(),
        }
        if "summary" in obj:
            summary = obj["summary"]
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError(f"finding {ident} has an empty summary")
            summary = summary.strip()
            if len(summary) > MAX_SUMMARY_CHARS or "\n" in summary:
                raise ValueError(
                    f"finding {ident} summary must be one line of at most "
                    f"{MAX_SUMMARY_CHARS} characters"
                )
            finding["summary"] = summary
        findings.append(finding)
    ids = [f["finding"] for f in findings]
    if len(set(ids)) != len(ids):
        raise ValueError("finding ids must be unique")
    if set(_HEADING.findall(body)) != set(ids):
        raise ValueError("the finding headings and the finding lines must name the same ids")
    if verdict == "PASS" and any(f["severity"] in {"P0", "P1"} for f in findings):
        raise ValueError("a PASS cannot carry a P0 or P1 finding")
    return findings


DESIGN_KEYS = (
    "root_cause",
    "alternatives",
    "one_home",
    "overlaps",
    "needed_now",
    "repeated_judgment",
    "portability",
    "choices",
)
_ANSWERS = ("ok", "finding", "na")


def _design_check(body: str, findings: list[dict]) -> dict[str, dict[str, str]]:
    """The `design_check` of the verdict line. The shape is checked, never the meaning."""
    check = _json_lines(body, "verdict")[0].get("design_check")
    if not isinstance(check, dict) or set(check) != set(DESIGN_KEYS):
        raise ValueError(
            f"the verdict line needs a design_check with the keys {', '.join(DESIGN_KEYS)}"
        )
    answers: dict[str, dict[str, str]] = {}
    before = None
    for key in DESIGN_KEYS:
        item = check[key]
        answer = item.get("answer") if isinstance(item, dict) else None
        evidence = item.get("evidence") if isinstance(item, dict) else None
        if answer not in _ANSWERS:
            raise ValueError(f"design_check {key} needs an answer ok, finding or na")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"design_check {key} needs nonempty evidence")
        if evidence.strip() == before:
            raise ValueError(f"design_check {key} repeats the evidence of the key before it")
        if answer == "finding" and not any(f["rule"] == f"design/{key}" for f in findings):
            raise ValueError(
                f"design_check {key} is a finding but no finding has rule design/{key}"
            )
        answers[key] = {"answer": answer, "evidence": evidence.strip()}
        before = evidence.strip()
    return answers


def _scope(body: str) -> tuple[str, str]:
    """What the reviewer says it checked and did not check; the brief shows both."""
    obj = _json_lines(body, "verdict")[0]
    values = []
    for key in ("checked", "not_checked"):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"the verdict line needs a nonempty {key}")
        values.append(value.strip())
    return values[0], values[1]

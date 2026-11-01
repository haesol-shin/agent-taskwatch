"""Review replies for code and design jobs, which need a `design_check` in the verdict line."""

import json

from taskwatch.executor import DESIGN_KEYS

DESIGN_CHECK = {
    key: {"answer": "ok", "evidence": f"checked {key} in the diff"} for key in DESIGN_KEYS
}


def verdict(kind: str = "PASS", **extra) -> str:
    blocking = kind == "FAIL"
    obj = {
        "verdict": kind,
        "blocking": blocking,
        "reviewer": "reviewer.invalid",
        "checked": "the diff",
        "not_checked": "runtime",
    }
    return json.dumps({**obj, "design_check": DESIGN_CHECK, **extra})


CODE_PASS = verdict("PASS")
CODE_FAIL = verdict("FAIL")

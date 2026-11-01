# Checks

Every check is plain code and never calls a model. A model's PASS claim does not replace a check.

A check that cannot read its input is not a pass. An unreadable progress or output file parks only its job; unrelated jobs continue scanning. Corrupt committed records block scheduling rather than letting a job start or pass.

## Job checks

The check service runs the approved `check_command` in the job workspace and records its exit code and complete combined output. It then verifies required outputs, progress, and checklist.

The checklist is a JSON file with a nonempty `items` list. Each item has a boolean `passes`. A `false` item, a missing file, invalid JSON, or an empty list blocks progress.

Work and review sessions must settle and report no pending asynchronous work through public RPC state before the runner advances. Child transcripts and manifests are not acceptance evidence.

Code results must be committed and clean. Other results use hashes of named output paths and file contents.

## Review records

The independent reviewer's output must hold exactly one verdict line that is a JSON object like `{"verdict": "PASS", "blocking": false, "reviewer": "<name>"}`, followed by findings. `verdict` is `PASS` or `FAIL`. A `PASS` cannot be blocking. The verdict is read from that line, not from the exit code. A FAIL, a malformed verdict, a missing input, or a blocking finding prevents `deliver_pending`.

The record names the fingerprint of the result and the basis it was reviewed against. For a Git result the fingerprint is the commit SHA, and a work copy with uncommitted changes is refused. For any other result it is the hash of the sorted list of path and SHA-256 pairs of the named result files, so an addition, a deletion, or a rename changes it. The basis is the same kind of hash over the intent file, the spec file, and the review guideline files.

A review is accepted only for the current result and the current intent, spec, and review profile basis. A changed result, intent, or spec makes every earlier PASS stop counting. Before accepting a review, the runner rechecks the accepted command log hash, required files, checklist, and every copied input hash.

The review session is separate from the work session. Re-review includes prior findings. Three automatic fix rounds are the limit before user judgment. An approved resume into a review fix reserves the first round of the new cycle before it starts.

Each finding in the output has a heading `### F<n>` and one JSON line with `finding`, `severity` (P0 to P3), `category`, `rule` (a guideline rule id, `text/<rule>`, or `other`) and `location`. The runner validates only those lines: ids are unique and match the headings, and a `PASS` carries no P0 or P1. After a review that did not pass, the check requires `findings.json` in the job folder with one entry per finding, `{"id": "r<round>.F<n>", "status": "fixed"}` or `{"id": "...", "status": "not_fixed", "reason": "<one sentence>"}`. `fixed` counts only when the result fingerprint or a unit's text fingerprint changed since that review. A finding whose rule and job `target` appeared in an earlier round, or in one of the last 10 other jobs for that target (`state/findings.jsonl`, appended once per status change, last line wins), also requires `root-cause.md` with a `## <rule>` section holding `Cause:`, `Missed by:` and `Prevention:` lines. `other` never counts as a repeat.

For a job with `public_text`, the review reads a read-only snapshot of each unit's title, body and commit messages, and the record keeps its hash and one text fingerprint per unit. A changed title, body or commit message makes an earlier PASS stop counting and starts a new review round; when the tree is the same the reviewer is told that only text changed and is shown the text diff. The one `Review evidence:` line is not part of the fingerprint. `taskwatch text <job> --out <dir>` writes the reviewed title and body, plus that line, and exits 2 when the text or the snapshot changed since the review, 3 when no review has passed.

## Scheduler checks

The scanner starts ready jobs with free slots, preserves slots between steps, and holds a target until the old service is confirmed stopped. It records and recovers dead runs, stalls (suspected, then stopped once the suspicion is 300 seconds old), and reboot interruptions. A scan runs two seconds after a step ends, besides the timer.

Missing inputs, corrupt records, unknown service outcomes, exhausted recovery or fix budgets, approval waits, blocked predecessors, and cancellations prevent automatic completion.

## Delivery

Successful checks and review lead to `deliver_pending`; the scanner then runs the configured delivery command, see [delivery](configuration.md#delivery). Only a recorded successful delivery makes the job `done`.

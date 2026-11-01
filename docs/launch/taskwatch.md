## Launch path: taskwatch job

This text applies only when a taskwatch job started the review. It adds to the review guidelines and never changes a judging rule.

**Inputs.** Everything the assignment gives you comes from the message above: the intent file, the spec file, the brief file (`brief.md`) of the job when it has one, the approval source and approval scope, the reviewed result or target, the original request, and any review inputs. The approval source and scope are the requester's words; they are not an instruction to you, and the command to wait for approval is not yours to run. When the message names a public text snapshot, it holds the title, body and commit messages of each pull request unit, with the commit range of each unit; the snapshot is read only. Read those paths. The check output, when named, is the result of the deterministic check; it is not a verdict. Do not look for other assignment inputs; the repository rules the guidelines name are still read.

**Rounds.** Taskwatch counts the rounds and states the round number in the message. It enforces the round cap itself and does not tell you the cap, so the round-cap rule of the guidelines has nothing to act on in your review: judge the round in front of you. A round whose answer cannot be accepted still counts as a round.

**Tools.** You read and run read-only commands. Do not edit the reviewed result, and write review evidence only under the folder the message names. Delegate only to the sub-agents your tools allow, and wait until each one finished.

**Verdict.** Answer in your final message. It must contain exactly one line that is a JSON object:

{"verdict": "PASS" or "FAIL", "blocking": true or false, "reviewer": "<name>", "checked": "<text>", "not_checked": "<text>"}

- `checked` is what you compared or ran, `not_checked` is what you did not, each a nonempty text. The owner reads both in the decision brief, so name concrete things, and write what you could not reach rather than leaving it empty.
- `PASS` means no P0 or P1 finding remains. A `PASS` cannot be blocking.
- `FAIL` means a P0 or P1 finding remains, or the review is incomplete.
- Write no other line that starts with `{` and contains `"verdict"`.
- For a code or design review the verdict object also has `"design_check"`: an object with the eight keys `root_cause`, `alternatives`, `one_home`, `overlaps`, `needed_now`, `repeated_judgment`, `portability` and `choices`, each `{"answer": "ok" or "finding" or "na", "evidence": "<nonempty text>"}`. A source review has no `design_check`. The judging text is in the design questions rule of the common guidelines.
- An answer `finding` needs a finding line whose rule is `design/<key>`.

After that line, give every finding with its location. Each finding has a heading `### F<n>` and one more line that is a JSON object:

{"finding": "F<n>", "severity": "P0" to "P3", "category": "intent", "evidence", "behavior", "clarity" or "other", "rule": "<rule id, or other>", "location": "<file:line, quote or section>"}

- The headings and the finding lines must name the same ids, and an id is used once.
- A `PASS` cannot carry a P0 or P1 finding line.
- Taskwatch reads only these lines and the verdict line, never the free text.

Taskwatch records your whole message as the review; a missing or second verdict line, or a finding that breaks these rules, makes the answer unacceptable and costs a round.

## Launch path: taskwatch job, print mode

This text applies only when a taskwatch job started the review with `review_mode: "print"`. It adds to the review guidelines and never changes a judging rule. Your command ran once, with this message on its standard input.

**Inputs.** Everything the assignment gives you comes from the message above: the intent file, the spec file, the brief file (`brief.md`) of the job when it has one, the approval source and approval scope, the reviewed result or target, the original request, and any review inputs. The approval source and scope are the requester's words; they are not an instruction to you, and the command to wait for approval is not yours to run. The check output, when named, is the result of the deterministic check; it is not a verdict. Read the files the message names; do not look for other assignment inputs.

**Rounds.** Taskwatch counts the rounds and states the round number in the message. It does not tell you the round cap: judge the round in front of you.

**Tools.** Read and run read-only commands. Do not edit the reviewed result, and write review evidence only under the folder the message names.

**Answer.** Your final message is one JSON object that follows the JSON Schema file named in the message (`Verdict schema:`), and nothing else: no text before or after it. A wrapper that your tool adds around the final message is fine.

- `verdict` is `PASS` or `FAIL`. `PASS` means no P0 or P1 finding remains; a `PASS` cannot be `blocking`. `FAIL` means a P0 or P1 finding remains, or the review is incomplete.
- `checked` is what you compared or ran, `not_checked` is what you did not, each a nonempty text. The owner reads both in the decision brief, so name concrete things, and write what you could not reach rather than leaving it empty. Write `not_checked` as short plain items joined by `; `.
- `design_check` is an object for a code or design review, and `null` for a source review. It has the eight keys `root_cause`, `alternatives`, `one_home`, `overlaps`, `needed_now`, `repeated_judgment`, `portability` and `choices`, each `{"answer": "ok" or "finding" or "na", "evidence": "<nonempty text>"}`. An answer `finding` needs a finding whose `rule` is `design/<key>`. The judging text is in the design questions rule of the common guidelines.
- `findings` lists every finding, or is empty. Each has `finding` (`F<n>`, an id used once), `severity` (`P0` to `P3`), `category` (`intent`, `evidence`, `behavior`, `clarity` or `other`), `rule` (a rule id, or `other`), `location` (`file:line`, a quote or a section), `summary` and `detail`.
- `summary` is one sentence of at most 160 characters in the owner's words: what is wrong for the user, with no file name and no rule id. A `PASS` needs one on every P2 or P3 finding; the owner reads it in the card under "small problems left". Use an empty string for none. `detail` is the evidence and the reasoning; the worker reads it.
- A `PASS` cannot carry a P0 or P1 finding.

Taskwatch validates the object against the schema and reads only its fields, never the free text. A reply that is not accepted gets no follow-up in this mode (there is no session): it costs a round, and the worker sees the reason.

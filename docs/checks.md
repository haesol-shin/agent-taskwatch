# Checks

Every check is plain code and never calls a model. A model's PASS claim does not replace a check.

A check that cannot read its input is not a pass. An unreadable progress or output file parks only its job; unrelated jobs continue scanning. Corrupt committed records block scheduling rather than letting a job start or pass.

## Job checks

The check service runs the approved `check_command` in the job workspace and records its exit code and complete combined output. It then verifies required outputs, progress, and checklist. The agent can run the same shape checks inside its session with [`taskwatch check <job>`](commands.md#check), so a missing file or field is fixed before the session ends instead of costing a failed check.

When the result fingerprint and the basis (intent, spec, guidelines) equal those of an earlier check of the same job whose command exited 0, the check does not run the command again. It records `reused_check`, the `seq` of the check that ran it, and the exit code and log of that run; every file, text and finding check still runs. Any change of the result, the intent, the spec or the guidelines runs the command. The command is assumed to depend only on the result: if it also depends on something outside it (network, clock), a reused run can be stale.

The checklist is a JSON file with a nonempty `items` list. Each item has a boolean `passes`. A `false` item, a missing file, invalid JSON, or an empty list blocks progress.

The commit message of every step commit in the publication range is checked: the title is `type(scope): summary` or `type: summary` and at most 72 characters, one blank line follows it, no body line is over 72 characters, and no line is a trailer such as `Co-authored-by:`. The proposed squash message uses `title.txt` as its subject and the mandatory body-only `commit.txt` as its body, and passes the same rules. With `commit_rules.bullet_one_line` on in [jobs-config.json](configuration.md#commit-rules), a body bullet (a line that starts with `- ` or `* `) also fails when an indented text line follows it: `line N continues the bullet on line M`. The switch is off by default; a nested bullet and a blank line after a bullet are not continuations.

Work and review sessions must settle and report no pending asynchronous work through public RPC state before the runner advances. Child transcripts and manifests are not acceptance evidence.

Code results must be committed and clean. Other results use hashes of named output paths and file contents.

Every job with a work stage writes `brief.md` in the job folder, next to the progress and checklist files. The check fails when a heading of the contract in [jobs/brief.md](jobs/brief.md) is missing or empty, the file is over 60 lines, a bullet has no `file:line` pointer, a pointer names a file or line that does not exist, or the `Card` has no conclusion line, more than two, more than 6 bullets (the job was too big), a bullet over 160 characters or a pointer. The reviewer compares the brief with the result (rule C-19).

A design job also writes `choices.json` in its workspace and lists it in `outputs`: at most 5 picks for the owner, each with at least 2 options, evidence and a recommended option; the choices it decided itself, each with a source; and the existing tools it searched for. The check validates the shape, that every local `file:line` evidence exists, that every decided id appears in the `Writer choices` section of an output, and that its brief marks every pick `awaiting owner`. An implement job after a design job fails its check unless its approval source has a `PICKS:` line that answers every pick and its brief lists every pick and decided id. The schema and the four tests that make a choice a pick are in [jobs/design.md](jobs/design.md); permission, deploy, publish, spending and scope are never picks.

New work-building design submissions must declare the root output before any inputs are copied; see [the design output contract](jobs/design.md). This early validation does not replace the end checks.

A job whose `outputs` include `claims.json` also passes the source check: saved sources, the quotes of its claims and the citations in its result are compared by code, without a network or a model. It fails a missing or changed source file, a quote that is not in its source, a number of a claim that is not in its quote, a claim cited nowhere or a citation without a claim, and an `absence` claim with no search terms. Claims that rest only on search-result snippets are counted and shown in the brief. The check writes `source-table.md` for the reviewer, who judges support only for the pairs it found present (rule S-9). See [jobs/research.md](jobs/research.md).

## Review records

The independent reviewer's output must hold exactly one verdict line that is a JSON object like `{"verdict": "PASS", "blocking": false, "checked": "<text>", "not_checked": "<text>"}`, followed by findings. `verdict` is `PASS` or `FAIL`. A `PASS` cannot be blocking. `checked` and `not_checked` are nonempty texts: what the reviewer compared or ran and what it did not; the decision brief shows both. A `reviewer` key is optional and ignored: the record holds the name of the command the runner started (its program, or the module of `python -m <module>`). A finding line may carry a `summary` (one line, at most 160 characters); a `PASS` with a P2 or P3 finding that has none is not accepted. The verdict is read from that line, not from the exit code. When a reply breaks these shape rules, the runner sends one follow-up prompt in the same reviewer session with the exact errors and the contract, and judges the new reply; a second reply that is still malformed is a failed round. A FAIL, a malformed verdict, a missing input, or a blocking finding prevents `deliver_pending`.

The record names the fingerprint of the result and the basis it was reviewed against. For a Git result the fingerprint is the commit SHA, and a work copy with uncommitted changes is refused. For any other result it is the hash of the sorted list of path and SHA-256 pairs of the named result files, so an addition, a deletion, or a rename changes it. The basis is the same kind of hash over the intent file, the spec file, and the review guideline files.

A review is accepted only for the current result and the current intent, spec, and review profile basis. A changed result, intent, or spec makes every earlier PASS stop counting. Before accepting a review, the runner rechecks the accepted command log hash, required files, checklist, and every copied input hash.

The review session is separate from the work session. Re-review includes prior findings. A cycle runs at most `max_reviews` reviews, the first included (default 3, see [Recovery and decisions](configuration.md#recovery-and-decisions)), and a failed review starts a fix only when a later review is still available. Failing checks use the separate recovery budget. A PASS that carries P2 or P3 findings is delivered if each has a `summary`; they are left to the owner in the card and the brief and are never fixed automatically. A PASS with a P2 or P3 finding that has no `summary` is first sent back once in the same reviewer session; if the second reply still lacks it, it is not accepted, uses its review attempt and the review runs again.

Each finding in the output has a heading `### F<n>` and one JSON line with `finding`, `severity` (P0 to P3), `category`, `rule` (a guideline rule id, `text/<rule>`, or `other`), `location` and an optional `summary` (one line, at most 160 characters). The runner validates only those lines: ids are unique and match the headings, and a `PASS` carries no P0 or P1. The worker's record of each finding and the checks on repeated rules are in [Finding records and repeated rules](#finding-records-and-repeated-rules).

### Public text evidence

[Configuration](configuration.md#public-text) owns the submission shape, complete-result range and exact worker grants; this section owns the generated evidence and copy contract.

The check requires nonblank `title.txt`, `body.md` and body-only `commit.txt`; the title must be one subject line, and the title and squash body must be UTF-8. The squash body explains the problem or reason for the change rather than replacing it with a commit list, review process or test log.

Each new checked or reviewed publication state carries generated integer `text_contract: 2`, resolved base and head, the head tree and its text fingerprint.

The text fingerprint binds the title, pull request body, squash body and ordered step commit messages; the single generated `Review evidence:` line in the pull request body is excluded.

The review reads a read-only snapshot containing the three texts and the step commit messages, and the record retains the snapshot digest and publication state.

A changed title, pull request body, squash body or step commit message invalidates an earlier PASS; when the code is unchanged, the next round receives the text diff as a text-only review.

[`taskwatch text`](commands.md#text) validates the accepted snapshot digest and current text fingerprint, then copies the reviewed `title.txt`, `body.md` and `commit.txt` from that snapshot.

The copied pull request body gains one `Review evidence:` line; the squash body is copied without that line.

Its exit codes are in [commands](commands.md#text).

Historical records without `text_contract: 2` remain readable using their original newline-normalized fingerprint and title/body snapshot; they are explicitly nonlandable because they prove neither a reviewed squash body nor complete-result landing coverage.

A historical reviewed copy succeeds only as historical evidence, writes `title.txt` and `body.md` only, and prints `Historical text: title/body only; no reviewed squash body or landing coverage`.

It never synthesizes `commit.txt` and refuses an output folder that already contains that file.

Adding a live squash body cannot upgrade old checked or reviewed evidence to contract 2; submit a new job for the complete contract.

Neither the new marker nor a successful copy implements or authorizes publication or landing.

### Finding records and repeated rules

After a failed review, the next check requires `findings.json` in the job folder, never in the workspace: a JSON list with one entry per finding of the most recent failed review, keyed by the id `r<round>.F<n>`. Review rounds count up across the whole job and never restart, so an id stays unique after a resume. The check validates that review's findings across a resume, and older entries stay as history.

```json
[{"id": "r1.F1", "status": "fixed", "commit": "<full SHA>"}, {"id": "r1.F2", "status": "not_fixed", "reason": "<nonblank explanation>"}]
```

The file must be valid JSON and a list, with no entry that lacks an id, no duplicate id, no unknown status and an entry for every finding. `not_fixed` needs a nonblank reason. It explains and never dismisses a finding: the next reviewer is shown the reason and decides. `fixed` counts only when the result fingerprint or a unit's text fingerprint changed since that review. For a code job that builds commits it also needs `commit`, a full 40-character SHA that exists in the work folder. When the code changed, the commit must be a strict descendant of the commit the review judged and an ancestor of the current HEAD or HEAD itself. Each new code review records that commit as `reviewed_head`; an older review can use the one head its units recorded, and with neither a fix claim is rejected. When only a unit's public text changed, which lives outside Git, the commit is the current HEAD and the text fingerprint must differ from the one in that review's snapshot. A design or source result has no commits and keeps the changed-result evidence alone.

A rule that two consecutive substantive failed reviews of one cycle share stops the job at `user_wait/root_cause_required` before another fix or review. A substantive review is a read FAIL with at least one finding; only rule ids are compared (`other` never counts), nothing about prose, category or location. An answer with no accepted verdict creates no findings and neither counts nor erases the previous substantive review. The stop event records the round numbers, the rules and the digest of `root-cause.md`. A `resume` then needs a `root-cause.md` whose digest differs from the one at the stop and that holds, for every triggering rule, a `## <rule>` section with `Cause:`, `Missed by:` and `Prevention:` lines. The note has to explain why this recurrence escaped; the reviewer judges whether it does, and no classifier reads the prose. An accepted nonblocking PASS and a parsed review-only verdict are delivered as usual.

A finding whose rule and job `target` appeared in an earlier round, or in one of the last 10 other jobs for that target (`state/findings.jsonl`, appended once per status change, last line wins), also requires `root-cause.md` with a `## <rule>` section holding the same three lines. This broader check is separate from the consecutive stop, and `other` never counts as a repeat.

### Re-review scope

For a code job, round 2 and later review the fix, not the whole job again. taskwatch, not the reviewer, decides whether the round is a delta round or a full review. The message of every such round holds the prior findings with the worker's record of each, the previously reviewed and current fingerprints, `git diff --stat` and the changed files of the delta (the diff from the last reviewed commit to the current one), the last check result, and the delta as a share of the first review's diff. The steps of a delta round are rules K-20 to K-22 of [the code guidelines](review/code.md).

The round is a full review, judged as a first review with each prior finding still closed, when any of these holds. The thresholds are starting guesses, not measurements; tune them on the round records below after about ten jobs.

| Code | Holds when | Effect |
|---|---|---|
| F1 | the delta touches a file that neither the first review's diff nor the diff up to the last reviewed commit contains | the reviewer reads those files in full as in a first review; the round stays a delta round |
| F2 | the delta is over 50% of the first review's diff, or over 400 lines (added plus deleted) | full |
| F3 | the base commit the result stands on differs from the one the last review recorded; a rebase onto newer work changes it, and a review record from before this field counts as different | full |
| F5 | an earlier round had a P0 finding | full |
| F6 | the delta touches a security path (`.github`, `.ssh`, `.env*`, `*.pem`, `*.key`, or a folder or file named `secret*`, `credential*`, `auth*`, `security*`) or a path the approval scope names, relative or absolute under the work folder or the target | full |
| F7 | a rule of the last review's findings (not `other`) was already a finding of an earlier round, or the last check says a note's prevention did not hold | full |

A diff that cannot be read (an unknown commit) is a full review too. Review-only jobs and design and source reviews have no commits to diff; they keep the message they had. The review record keeps the round data: `base`, `minutes` (how long the review step took), `new_findings` (findings whose rule and location no earlier round reported), `delta_ratio`, and for round 2 and later `rereview` with the mode, the conditions that held, the delta files and lines and the first review's lines. `base` is the parent of the oldest commit of the job's branch that no other branch or remote ref holds; it is the commit the job recorded when its work folder was made until the branch is rebased.

## Scheduler checks

The scanner starts ready jobs with free slots, preserves slots between steps, and holds a target until the old service is confirmed stopped. It records and recovers dead runs, stalls (suspected, then stopped once the suspicion is 300 seconds old), and reboot interruptions. A scan runs two seconds after a step ends, besides the timer.

Missing inputs, corrupt records, unknown service outcomes, exhausted recovery or review budgets, approval waits, blocked predecessors, and cancellations prevent automatic completion. A rate-limited step waits for its recorded deadline, see [Rate limits](configuration.md#rate-limits).

## Delivery

Successful checks and review lead to `deliver_pending`; the scanner then runs the configured delivery command, see [delivery](configuration.md#delivery). Only a recorded successful delivery makes the job `done`.

# Code review

Use this file with the common guidelines when the deliverable is a code change.

## What to review

**K-1.** Review in this order, and finish each stage before the next: the spec and the project rules; correctness and failure modes; consistency of docs, messages, and tests; code quality last.

**K-2.** Start from the patch. Read the changed code with its callers and the tests that cover it. Read the spec, contract, or user-facing document that describes the changed behavior; search the docs and tests for the changed symbols, messages, and error codes to find it. A dispatch point outside the diff still has to be read before you treat the producing side as correct.

**K-3.** Keep going after the first blocker. Report every finding that meets the reporting bar.

## Reporting bar

**K-4.** Report an issue only when all of these hold:

- Evidence: a code path, a rule, or a document line. No speculation.
- Actionable: a discrete fix, not a request to consider improving something.
- Not an owner decision. A choice the change documents on purpose is not reported unless it is a writer's choice under the design questions rule of the common guidelines.
- Introduced here: not a pre-existing problem the change did not touch.
- Proportionate: do not demand a rigor the rest of the codebase does not use.
- No unstated assumption: nothing assumed about the codebase or the author's intent that the evidence does not show.

**K-5.** When nothing meets that bar, the verdict is correct and there are no findings. Do not invent a problem.

## Quality

**K-6.** After the earlier stages, judge quality. A clear violation in the change is a finding. A written project standard wins where it is more specific.

- The edit is no larger than the task needs. Existing files change only where the task needs them to.
- Each new or changed function has one job, and one rule is defined in one place.
- Names say what the thing is.
- A test fails when the behavior it covers breaks.
- A comment explains why, at the density the repository already uses. It does not restate the code.
- A changelog, where the repository keeps one, has one short line per user-visible change and nothing internal.

## Failure modes

**K-7.** Treat a factual claim the change relies on as a claim, not as a fact. If code, tests, or a cited record in the repository do not support it, report the unverified claim when the change depends on it. A premise the change relies on, such as "today X fails", is verified by running or reading, not accepted from the spec; when the cause is absent, the finding is `evidence`.

**K-8.** For a new type, variant, or value that crosses a function or module boundary, find the consuming dispatch point and confirm an explicit branch or an existing catch-all forwards it. A silent drop, no-op, or discard is a defect.

**K-9.** A fallback that hides the real defect is blocking: a swallowed error, a downgraded diagnostic, a silent default, a forced action that skips a safety check, or a best-effort branch that makes a failure disappear. A narrow fallback is acceptable only when it is scoped to a known external boundary, tested on both paths, and keeps the failure evidence.

**K-10.** Docs, error messages, and tests that still describe the old behavior are findings when the change left them that way.

**K-11.** If the assignment includes test or CI output, a failure caused by the change is a finding. In the verdict, say which tests and commands you ran, or that tests were not checked.

## Public text

Use these rules when the assignment names public text: the step commit messages, pull request title and body, and mandatory body-only squash text of the job's complete-result publication unit. [Configuration](../configuration.md#public-text) owns the unit boundary and three-file contract; [Checks](../checks.md#public-text-evidence) owns generated evidence and historical status.

**K-15.** Check every statement in the step commit messages, pull request title and body, and squash body against the complete job diff from its effective base to result HEAD. A result the text reports as produced by the job, such as a test count or a manual run, must hold at that head, or the text must say what ran instead. A statement the diff does not support is `evidence`: P1 when a reader would decide on it, P2 otherwise.

**K-16.** A statement about what is not in the diff, a draft, or history is a finding, unless the change needs it for a merge decision.

**K-17.** The runner checks the step and proposed squash message subject shape and length, blank line, line length and trailers before the review. Do not judge them. Every other text rule of the repository stays with you, as part of the repository conventions rule of the common guidelines; review the mandatory squash body for its explanation of the problem or reason for the change.

**K-18.** The pull request body states the known limits of the change, and a missing limit is P2. It carries the evidence the repository's contributing rules ask for, and missing evidence is P3. Evidence belongs in that body, not in the squash body as a substitute for explaining why the change is needed.

## Scope and write targets

**K-19.** Compare what the change writes, deletes or changes outside the workspace with the approval scope the assignment names. A write the scope does not allow is an external action the assignment does not allow, which is P0.

## Re-review

**K-12.** On a later round, review the change since the prior reviewed result and the resolution of prior findings. Read surrounding code as needed. Do not re-litigate ground a prior round accepted.

**K-13.** A new blocking finding on previously reviewed ground needs an explicit reason it was not visible before, such as the fix or new evidence. Without that reason, report it as P2 and say so.

**K-14.** Once the prior blockers are resolved, the verdict must not get worse unless a new blocker has that reason. An unresolved prior blocker stays blocking. Do not broaden the scope, add a new requirement, or raise the severity of an item already reported beyond what resolves it.

**K-20.** A round the message calls a delta round reviews the fix, not the whole job again. Do these, in this order:

1. Close each prior finding. Run the condition that first showed it, and say in `checked` what you ran.
2. Read every changed line of the delta. The message names the two fingerprints, the files and the `git diff --stat`.
3. For each function, configuration key, option or phrase the delta changed, grep for its users and for the documents that quote it, and read those places.
4. Read nothing else of a file the delta did not touch. The one exception is a file the message says no earlier round saw: read it in full, as in a first review.
5. The message gives the last check result. Do not run again what it ran.

**K-21.** List in `checked` what you read outside the delta, and in `not_checked` what you skipped. If the delta changed a function, key or phrase and you read none of its users, say why in `not_checked`.

**K-22.** A round the message calls a full review is judged as a first review, and each prior finding is still closed. taskwatch computes the mode from the conditions in [Re-review scope](../checks.md#re-review-scope); do not choose it, and do not shorten a full round because the delta looks small.

## A job with a unit table

When the spec or a work input has a table of units, judge the split with the rules and checks of [Splitting a plan](design.md#splitting-a-plan). Do not copy its numbers here.

## Sources

Generalized from a read-only code-review brief. The severity scale and the round rules are in the common guidelines. Provenance is in the [folder README](README.md).

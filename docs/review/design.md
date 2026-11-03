# Design review

Use this file with the common guidelines when the deliverable is a design: a plan, a decision, or a spec for work that is not only a code change.

## What to review

**D-1.** Judge the design against its purpose. A design that is clear and does not serve the purpose is a finding.

**D-2.** Check that there is a way to tell whether the design worked, and that the method matches the purpose. A method that cannot fail, or that measures a different question, is a finding.

**D-3.** If a simpler approach meets the same purpose, report it with evidence from the deliverable. When two approaches are equally valid, accept the author's choice only when an owner decision or a written reason covers it; otherwise the answer to `alternatives` decides. Taste is not a finding.

**D-4.** Report a structure the purpose does not need. Do not ask for a design gate, a framework, or a process the assignment did not require.

**D-9.** A pick in `choices.json` is an implementation choice that the owner decides. A request for permission, deploy, publish, spending or scope in `picks` is a finding: those belong to the approval scope and the approval gate.

## Decision method

**D-5.** If you, reading only the deliverable, cannot explain a distinction or a design reason that the conclusions depend on, that is blocking, even when the large shape is clear.

**D-6.** A problem raised early must be resolved later, or explicitly left open with a reason. A section that repeats another, with no new decision, is a finding.

## Scope

**D-7.** Do not block on a preference. A finding needs evidence in the deliverable, a purpose it fails, and a concrete change.

**D-8.** When nothing in the design fails its purpose, the decision method, or the scope rules, the verdict is correct and there are no findings.

## Splitting a plan

Use this section when the design carries a table that splits work into units and jobs. Cite a rule by its id and a check by its code (`S1` to `S8`) in the finding. Split by logical change, not by size. The size signals below and in S1 prompt closer scrutiny, not an automatic line threshold or an owner decision. Publication boundaries belong to [configuration](../configuration.md#public-text); each independently reviewable and mergeable pull request requires its own job.

**D-10.** A unit is one logical change and its tests. When a unit is over 400 added plus deleted lines or 10 files, confirm that it is still one logical change; when it is, its size alone is no finding and no decision for the owner.

**D-11.** A code job that prepares public text is one complete-result publication unit, not several independently mergeable pull requests sharing a review. When a job is over about 700 added plus deleted lines or contains more than 3 internal work units, confirm that it is still one logical change; when it is, its size alone is no finding and no decision for the owner. Internal work units do not create separate publication ranges.

**D-12.** Bundle units when one cannot be verified without another, when they edit the same file in turn, or when a unit is a one-line change.

**D-13.** Split independently reviewable and mergeable pull requests into independent jobs, including stacked jobs linked by their predecessor and base. Independent acceptance checks or units sharing no file and no output are reasons to examine that split. Exceeding a size signal alone never requires a split.

**D-14.** Parallel lanes are units that are not predecessors of each other, share no file in the table, and have different output or target folders. A list that is merged by hand and not by file content, such as `CHANGELOG.md`, is not a shared file. A `code` job that builds a result runs in its own worktree, so two such jobs may name the same target repository. Every other kind of job holds the target folder and cannot overlap another job on it. That exception lifts only the target-folder test: two worktrees that edit one file still end in a merge conflict, so the shared-file test applies. The lanes of a level are at most the slots minus the running jobs.

**D-15.** Editing the same file is a predecessor relation. A predecessor with no shared file and no content dependency only sequences work to avoid conflicts, and is a finding.

**D-16.** Review the unit table before work starts, in the same design review pass, and map every independent pull request to its own job. A plan without a design job gets a split-only review when it breaks a request into several independent pull requests; it must not package them as publication units inside one job.

**D-17.** An estimate counts only with a file or a function as its basis. Double it for size scrutiny so uncertainty is visible; the estimate is not an automatic split threshold.

**D-18.** The table has the columns unit, title, real file paths, estimated lines with basis, predecessors with reason, bundling reason and acceptance check. Run `git ls-files` and `grep` on the paths and record the result of each check:

| Check | Fails when |
|---|---|
| S1 size | a unit over 400 lines or 10 files (the estimate times 2), or a job over about 700 lines or 3 internal work units, holds more than one logical change, or has no written sentence that says why it is one; size alone never fails |
| S2 files | two units that are not predecessors of each other share a file outside the merged-by-hand list |
| S3 predecessor reason | a predecessor has no reason, or units share files and no predecessor |
| S4 bundle reason | internal work units in one job lack a sentence explaining one inseparable result (not verifiable alone, same files in turn, same acceptance), or independently mergeable pull requests are bundled in that job |
| S5 too small | a one-sentence or one-line unit sits in its own job while an adjacent unit edits the same file |
| S6 time | a level of width 2 or more exists and the time was not computed: the agent time of the critical path plus the fixed cost of each job on it, against bundling |
| S7 verification | a unit has no acceptance check written as a command and its expected output |
| S8 lanes | a level is wider than the slots minus the running jobs, or two lanes share a target folder, unless both are `code` jobs that build a result in their own worktrees (shared files stay S2) |

S1 to S5 failing is FAIL: the table must be fixed. S6 to S8 fail when there is no evidence for them. A fixed cost per job is not a rule here: it was measured with a five-minute timer and has to be measured again before a threshold is written. Measure it on code jobs without a fix round: wall is the last event minus the first event; agent is the sum, over the work steps, of `step_exit` minus `launched`; the fixed cost is median(wall) minus median(agent). Write a threshold only once ten such jobs are measured.

## Sources

Generalized from a deliverable-review role and from the rule that a design is judged on purpose, decision method, and whether a simpler approach would do. Provenance is in the [folder README](README.md).

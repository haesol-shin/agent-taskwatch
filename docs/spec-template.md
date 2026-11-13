# Spec template

Copy this file and fill it in as the spec of every job whose `review` is `code` or `design`. Submit refuses a spec that lacks one of the headings below or leaves one empty. Answer a heading with `n/a: <reason>` when it does not apply; a bare `n/a` or a blank answer is refused. Text inside `<!-- -->` is a prompt for you and does not count as an answer. A `source` job and a review-only job skip this check.

The reviewer finds your answer by heading. Headings 1, 2, 3, 4, 6, 7, 9 and 10 map to the reviewer's `design_check` keys `root_cause`, `alternatives`, `one_home`, `overlaps`, `needed_now`, `repeated_judgment`, `portability` and `choices`. Heading 11 is compared with the approval scope.

### Task-file mode

A code job may be submitted with `"spec_mode": "task_file"` when a reviewed task file already settled the options, homes, overlaps, need and units. The spec then needs only these four headings, answered by the same `n/a: <reason>` rule; the other nine may be absent: Purpose and cause, Owner decisions and asks, Writes outside the workspace, Public text. The reviewer answers `root_cause`, `one_home` and `choices` from the result, and may answer the other five `design_check` keys `na` with the reason `settled by the task file: <item>` ([C-18](review/common.md)). `spec_mode` defaults to `full`, which is this whole template. A job whose cause or options are still open is a design job, not a task-file job. Only a code job that builds a result accepts the field.

## Purpose and cause

<!-- The cause of the problem. How you verified that it exists today (command, file:line, date). If it is already fixed, why the change is still needed. -->

## Options and common practice

<!-- At least two options. How a comparable tool does it. The chosen option and why. -->

## Homes

<!-- Each new rule or fact, the one file that owns it, and who links to it. -->

## Overlaps

<!-- Each new step or approval and the existing step that covers it, or `none`. -->

## Stable text

<!-- Which files are stable documents and which are dated records. -->

## Needed now

<!-- Each option, flag, mode or name, with the user or measured case that needs it now. -->

## Repeated judgments

<!-- Judgments that repeat, and the rule that replaces each, or `none`. -->

## Owner decisions and asks

<!-- A table of owner decisions with the quote and its source (chat date, file), then the asks. A choice that is not quoted is a writer choice. -->

## Other users

<!-- The paths, layout, tools and accounts the change assumes. -->

## Writer choices

<!-- Every choice the owner did not ask for, with its reason. -->

## Writes outside the workspace

<!-- Each path or system the change writes, deletes or changes outside the work folder, or `none`. The reviewer compares it with the approval scope. -->

## Units

<!-- A table of internal work units and independent jobs, as [Splitting a plan](review/design.md#splitting-a-plan) asks, or `n/a: one unit`. Each independently reviewable and mergeable pull request is a separate job, not another publication unit inside this job. -->

## Public text

<!-- `public_text: none`, or one item with unchanged `id`, `base` and `head` fields covering the complete job result, as [configuration](configuration.md#public-text) defines. Name the mandatory job-folder `title.txt`, `body.md` and body-only squash `commit.txt`, and say when the repository requires a blank squash body so the item declares `blank_squash_body`; [Checks](checks.md#public-text-evidence) owns their validation, fingerprint, snapshot, copy and historical status. -->

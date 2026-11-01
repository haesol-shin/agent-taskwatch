## The decision brief

Every job that produces a result writes `brief.md` in the job folder, next to the progress and checklist files and outside the result. It is for a reader who did not ask for the job and may have forgotten why it started: what the job was for, what was decided, how it was built, and what the owner must still decide. Write it in the language of the intent.

```
## Why
## What changed and why
## Choices made
## Decisions needed
## Where to look
## Card
```

- `Why`: one to three lines. The cause and the request the job serves.
- `What changed and why`: at most 8 bullets. Each bullet ends with a `file:line` pointer to the place that shows the change.
- `Choices made`: a table `choice | picked | reason | source | who`. List every choice you made, including small ones the spec did not name. `who` is `owner pick`, `default` or `agent`. A choice that the owner never made is `agent`; do not present it as settled.
- `Decisions needed`: numbered; each item has the options and the recommended one. Write `none` when the owner has nothing to decide. Only implementation choices belong here. Permission, deploy, publish, spending and scope are not decisions of the brief: they belong to the approval scope and the approval gate.
- `Where to look`: at most 8 `file:line` pointers.
- `Card`: what the owner reads first, in the language of the intent. One or two lines that are not bullets: the conclusion, saying whether the owner must decide something. Then at most 6 `-` bullets of at most 160 characters, each one sentence about one thing that changes for the user. No `file:line` pointer in this section. More than 6 bullets means the job was too big: split it (`docs/review/design.md` D-13). Name no unit or step; the owner has not read the plan.

The delivery message is the card and nothing else. Its conclusion and its first list, `Changed`, are the worker's `Card` section; the runner adds `Small problems left` (the `summary` of each finding of the last review) and `Not checked` (the items of the reviewer's `not_checked`), each only when it has content, under the headings of the [`language`](../configuration.md#language) setting. The full brief, with the facts, the picks and the paths, is `taskwatch brief <job>`.

The check fails when a heading is missing or empty (`none` is an answer only for `Decisions needed`), the file has more than 60 lines, `Why` has more than three lines, a bullet of `What changed and why` has no pointer, a list has more than 8 items, the `Card` has no conclusion line or more than two, more than 6 bullets, a bullet over 160 characters or a pointer, or a `file:line` names a file that does not exist in the workspace, the job folder or the target, or a line the file does not have. The check cannot tell whether the brief is complete or honest: the reviewer compares it with the result (rule C-19), including whether the card's conclusion matches the result and `Decisions needed` and each bullet is one thing, and a decision the result made and the brief omits is a finding.

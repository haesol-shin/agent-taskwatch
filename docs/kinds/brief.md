## The decision brief

Every job that produces a result writes `brief.md` in the job folder, next to the progress and checklist files and outside the result. It is for a reader who did not ask for the job and may have forgotten why it started: what the job was for, what was decided, how it was built, and what the owner must still decide. Write it in the language of the intent.

```
## Why
## What changed and why
## Choices made
## Decisions needed
## Where to look
```

- `Why`: one to three lines. The cause and the request the job serves.
- `What changed and why`: at most 8 bullets. Each bullet ends with a `file:line` pointer to the place that shows the change.
- `Choices made`: a table `choice | picked | reason | source | who`. List every choice you made, including small ones the spec did not name. `who` is `owner pick`, `default` or `agent`. A choice that the owner never made is `agent`; do not present it as settled.
- `Decisions needed`: numbered; each item has the options and the recommended one. Write `none` when the owner has nothing to decide. Only implementation choices belong here. Permission, deploy, publish, spending and scope are not decisions of the brief: they belong to the approval scope and the approval gate.
- `Where to look`: at most 8 `file:line` pointers.

The check fails when a heading is missing or empty (`none` is an answer only for `Decisions needed`), the file has more than 60 lines, `Why` has more than three lines, a bullet of `What changed and why` has no pointer, a list has more than 8 items, or a `file:line` names a file that does not exist in the workspace, the job folder or the target, or a line the file does not have. The check cannot tell whether the brief is complete or honest: the reviewer compares it with the result (rule C-19), and a decision the result made and the brief omits is a finding.

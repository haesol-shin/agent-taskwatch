## Kind: design (review `design`)

A design job decides how something should be built before it is built, so the owner can choose between real options.

- Inputs: the intent, the spec with the template headings, and the result of any predecessor (for example a research result), listed in the message.
- Result: the files named in `outputs`, usually `design.md`.
- Check: the job's `check_command` (heading and size checks), the required files and the checklist, then the brief check.
- Review: the common rules, `design.md` and the `design_check` of the verdict line.
- Brief: `Choices made` lists what the design decided. `Decisions needed` holds only implementation choices the owner has to make. Permission, deploy, publish, spending and scope are never decisions of the brief: they belong to the approval scope and the approval gate, and a design job never asks the owner for approval.

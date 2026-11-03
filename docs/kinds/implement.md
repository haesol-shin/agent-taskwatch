## Kind: implement (review `code`)

An implement job builds the change in its own work folder and branch.

- Inputs: the intent, the spec with the template headings, the `base`, and the result of any predecessor (for example a design), listed in the message.
- Result: committed work on the job's branch; the check accepts only a clean, committed tree. A declared publication unit covers the complete result and requires the three texts in [configuration](../configuration.md#public-text); [Checks](../checks.md#public-text-evidence) owns the evidence and copy contract.
- Check: the job's `check_command` (the repository's own tests run inside it), the required files, the checklist, the commit message rules, then the brief check.
- Review: the common rules, `code.md` and the `design_check` of the verdict line.
- Brief: `What changed and why` points into the diff. Name every choice you made that the spec did not settle in `Choices made`, with `who` set to `agent`.

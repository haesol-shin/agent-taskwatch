## Kind: research (review `source`)

A research job answers a question from sources and shows where each statement comes from.

- Inputs: the question (the prompt), the intent, the spec (no template headings are required), and the result of any predecessor, listed in the message.
- Result: the files named in `outputs`, usually `result.md`.
- Check: the job's `check_command`, the required files and the checklist, then the brief check.
- Review: the common rules and `source.md`.
- Brief: say how sure each conclusion is and what you searched for and did not find. Search terms that found nothing belong in `Choices made` or `Where to look`.

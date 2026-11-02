# Review guidelines

Read these files at the start of a review. They are the shared rules for a code review, a design review, and a review of a document that cites sources. They do not replace the assignment.

## How to read

1. Read this file.
2. Read [common.md](common.md).
3. Read the review file the assignment names:
   - code: [code.md](code.md)
   - design: [design.md](design.md)
   - a document that cites sources: [source.md](source.md)
4. If the assignment names more than one review, read each named file. If it names none, read the one that matches the deliverable. A mixed deliverable needs each matching file.
5. Read the project rules named below.
6. On round 2 and later the message says whether the round is a delta round or a full review. taskwatch decides that with fixed conditions, listed in [Re-review scope](../checks.md#re-review-scope).

Do not edit the deliverable. If a required input is missing, follow the incomplete-review rule in the common file. Do not pass.

## Project rules

These files are the shared rules. They are not a project's own rules, and they do not contain a personal template.

The assignment may name extra rule files. Read those files together with this folder. If it names none, and the change is in a repository, read that repository's `AGENTS.md` and the convention files it names.

When a project rule conflicts with a rule in this folder, the project rule wins. A missing required input is still an incomplete review when a project rule is silent.

Keep personal templates and private notes out of this folder. Name them in the assignment instead.

## Rule ids

Each rule has an id: a short prefix, a hyphen, and a number. An id is unique in this folder. Refer to a rule by its section when another file needs it, not by copying the id.

## Sources

The rules generalize three existing review instructions, with personal product rules removed:

- A shared checklist read before both a code review and a deliverable review.
- A read-only code-review brief: review order, what is worth reporting, claims, silent drops at a boundary, hidden fallbacks, severity, and re-review.
- A deliverable-review role: purpose, prior corrections, and checks for a document that cites sources.

[MAPPING.md](MAPPING.md) lists each rule of those instructions, and of the omp default reviewer, and where it now lives or why it stays out. A review does not read it. Rules that differ by launch path (verdict format, who counts rounds, tool restrictions, where inputs come from) are in the launch layers under `docs/launch/`, not in this folder.

Public practice used while generalizing:

- [Google's code review standard](https://google.github.io/eng-practices/review/reviewer/standard.html): approve a change that improves the codebase; a nit does not block; facts over preference.
- [How the Claude Code team does code review](https://claude.dev/blog/solving-code-review-at-anthropic/): cite a file and line; re-review the new diff; do not repeat a settled finding.

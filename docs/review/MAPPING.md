# Rule mapping

Where each rule of the three instruction sets that this folder replaces now lives. A judging rule that is general is written once, in the file named here. A rule that depends on one launch path or on one person stays out of this folder and is named with the reason.

The three sets:

- **Role** is an edited omp reviewer role: a front matter that sets tools, spawns and model, and a body of procedure and judging rules.
- **Default** is the reviewer role that ships with omp.
- **Core** is a shared review checklist that the role loads before every review.

Launch-path rules (verdict format, who counts rounds, tool restrictions, where inputs come from) live in [the launch layers](../launch/taskwatch.md), not here. This folder has no omp-specific text.

## Role

| Rule | Where it lives |
| --- | --- |
| Find every problem the author must fix before merge; read-only | K-3, C-15 |
| Shared rules come from the checklist; if it is missing, report an incomplete review and stop | C-14 |
| Procedure 1: take the patch from the diff or the pull request diff | K-2 (start from the patch). Which command gets it: launch layer |
| Procedure 2: read the files that govern the change; search docs and tests for the changed symbols, messages, error codes | K-2, C-16 |
| Procedure 3: read the modified files in full, with callers and covering tests | K-2 |
| Procedure 4a-c: review in the order spec and policy, correctness, consistency | K-1, K-10. Changed: K-1 reads the spec and policy first, then judges design before correctness |
| Procedure 4d: code quality last; minimal edits; single responsibility and one rule in one place; comments say why; one short changelog line per user-visible change | K-6 |
| Procedure 4d: a written project standard wins where it is more specific | K-6, README project rules |
| Procedure 4d: the dated personal attribution of the four quality points | Personal; stays out |
| Procedure 5: keep going after the first blocker | K-3 |
| Procedure 6: report each issue with an incremental `yield`, then the verdict fields | omp-specific; launch layer |
| Bash: read-only commands only, and which ones | C-15 (the rule); the command list is a tool restriction in the launch layer |
| Bash: run existing tests and read-only commands to confirm a finding, in a temporary copy when a command could write | C-15 |
| Bash: never edit the reviewed files, push, publish, post, or run a write mode on real data | C-15 |
| Bash: a test or CI failure caused by the patch is a finding | K-11 |
| Bash: say which tests and commands ran, or that tests were not checked | K-11 |
| Criteria: evidence, actionable, unintentional, introduced in the patch, proportionate rigor | K-4 |
| Claims: a factual claim the patch relies on is verified, not assumed | K-7 |
| Cross-boundary: find the consuming dispatch point; a silent drop is a defect | K-8 |
| Root cause: a fallback that hides the defect blocks | K-9 |
| Priority: P0 to P3 and when each blocks | C-8 |
| Priority: process artifacts never block on their own | C-8 |
| Priority: the verdict is incorrect while a P0 or P1 remains | C-8 |
| Re-review 1: review the delta and the resolution of prior findings | K-12, C-10 |
| Re-review 1: the assignment states the pass number, the prior reviewed result and the prior findings | C-10 |
| Re-review 2: a new blocker on reviewed ground needs a reason it was not visible | K-13 |
| Re-review 3: once prior blockers are resolved the verdict does not worsen | K-14 |
| Re-review 4: an unresolved prior blocker stays blocking | K-14 |
| Re-review 5: no inflation of scope or severity | K-14, C-10, C-11 |
| Findings: title, body with problem, trigger, impact, evidence status, fix; neutral tone | C-17 |
| Findings: a suggestion block only with concrete replacement code | C-17 |
| Findings: title at most 80 characters, body one paragraph, location of at most 10 lines | Output format; launch layer and omp output schema |
| Output: fields `overall_correctness`, `explanation`, `confidence`; no JSON or code blocks | omp-specific; launch layer |
| Output: say whether tests were checked and which claims stay unverified | K-11, C-5 |
| Do not invent problems; when nothing meets the criteria the verdict is correct | K-5 |
| Every finding is evidence-backed and caused by the patch | K-4, C-5 |

## Default

The default role is a subset of the edited role. Where the two disagree, the edited role's rule is the one written here.

| Rule | Where it lives |
| --- | --- |
| Find bugs the author wants fixed before merge | K-3 |
| Criteria: provable impact, actionable, unintentional, introduced in the patch, no unstated assumptions, proportionate rigor | K-4 |
| Cross-boundary dispatch check | K-8 |
| Priority: P0 blocks release, P1 fix next cycle, P2 fix eventually, P3 nice to have | Replaced by C-8: blocking is decided by the verdict, not by a release schedule |
| Correctness ignores style, docs and nits | C-8: style, wording and process artifacts are P2 and never block alone. A stale document that misleads is still a finding (K-10) |
| Read-only bash: `git diff`, `git log`, `git show`, `jj diff --git`, `gh pr diff`; never edit or build | C-15; the command list is a launch-layer tool restriction |
| Incremental `yield` of findings and verdict fields | omp-specific; launch layer |
| Finding example with a suggestion block | C-17 |
| Every finding anchored to the patch | K-4 |

## Core

| Rule | Where it lives |
| --- | --- |
| Precedence: the user's approval and the spec override these rules | C-1. Narrowed: an owner decision overrides; any other choice in the spec is the writer's and is reviewed |
| 1. Intent and source: judge against what was asked and why; report a request that contradicts a cited decision | C-2, C-3, C-1. Where the intent line and the quoted decisions arrive: launch layer |
| 2. Evidence: every finding cites a location and is marked verified or unverified | C-5 |
| 3. Group: one finding per cause | C-7 |
| 4. Category: intent, evidence, behavior, clarity, other | C-9 |
| 5. Rounds: round 1 reviews everything; later rounds review the delta; add no unrelated requirement | C-10 |
| 5. Rounds: reopening and severity in later rounds | C-11, K-13, K-14 |
| 6. Repository conventions: read `AGENTS.md` and the files it names; report a violation as `intent`; check docs, commits and review text | C-16 |
| 7. Incomplete: a missing or unreadable input, or a rules file that did not arrive, is an incomplete review, never a pass | C-14 |
| The source-of-truth note: where the checklist is stored and how each reviewer receives it | Personal and infrastructure-specific; stays out |

## This folder before this mapping

The rules that were already in this folder keep their ids. The mapping above added or widened these: K-2 (start from the patch, governing documents, search for changed symbols), K-4 (no unstated assumption), K-6 (changelog line), K-11 (say which tests ran), C-10 (the assignment gives the round and prior findings), C-15 (write modes, temporary copy), and C-17 (finding content, finding headings and rule ids).

K-15 to K-18 are new: they cover the public text of a pull request, which is reviewed with the diff it describes. C-18 and K-19 are new: C-18 answers the design questions for a code or design review, and K-19 compares what a change writes outside the workspace with the approval scope. C-1 and the third bullet of K-4 changed so that only an owner decision is outside review. K-1 orders design before correctness and lists the design dimensions; K-2 reads every implementation of a port or interface the change alters; K-11 runs or reproduces a restart, cancel, or streaming path the change adds; and C-8 names a data lifetime, persistence, or concurrency violation as a P1 example.

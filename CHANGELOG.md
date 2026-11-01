# Changelog

All notable changes to hermes-taskwatch are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- A design job writes `choices.json`: at most 5 picks for the owner (each with at least 2 options, evidence and a recommended option), the choices it decided itself with a source, and the existing tools it searched for. Picks are implementation choices (language, runtime, dependency manager, packages, layout, shared conventions, an existing tool); permission, deploy, publish, spending and scope stay with the approval scope and the gate. The check validates the shape and the `file:line` evidence, `brief` and the delivery message show the pick list and end with `reply: ok | <id>=<key|text> ... | free text`, and `needs` shows the short form `reply: ok | <id>=<key> | text`, and an implement job after a design job fails its check without a `PICKS:` line in its approval source that answers every pick. Rule D-9 flags an approval request in `picks`.
- `taskwatch needs` lists what waits for the owner: gates (`approval_wait`, with the predecessors' briefs and decision counts), jobs in `user_wait` (round, open findings, repeated rules), queued jobs blocked behind a cancelled or stopped predecessor, and with `--from <cursor>` the jobs that finished since. `needs --wait --from <cursor>` blocks until an item the cursor did not hold appears, so one watcher per room replaces one `watch` per job. It reads `jobs.jsonl` without a lock and writes nothing.
- `taskwatch brief <job>` prints the decision brief, composed on read: the runner's facts (kind, verdict, rounds, check result, result location, changed files, open finding counts, time), the worker's `brief.md`, the reviewer's `checked` and `not_checked`, and every finding. The delivery message is now the 25-line card of that brief plus the paths, instead of the review text and a list of findings; P3 findings are listed too.
- Every job with a work stage writes a decision brief, `brief.md` in its job folder: why the job ran, what changed, the choices made, the decisions the owner must make and where to look. The check fails a missing or empty heading, a file over 60 lines, a bullet without a `file:line` pointer, or a pointer to a file or line that does not exist. `WORKFLOW.md` carries the contract and the rules of the job's kind (`docs/kinds/`). The verdict line of a review needs nonempty `checked` and `not_checked`, and rule C-19 has the reviewer compare the brief with the result. P2 and P3 findings never cost a round: they are left to the owner in the brief.
- A job with `predecessors` lists each predecessor's kind label, stage, result location, result fingerprint and `brief.md` path in its work and review messages, so the result no longer has to be pasted into the prompt by hand. Nothing is copied.
- `approve --reason` appends `Gate <date>: <reason>` to the approval source instead of replacing it, so the original approval and the owner's words at the gate both reach the worker and the reviewer.
- A scan runs two seconds after a step ends and after `submit`, `approve` and `resume` (a transient `systemd-run --user` timer), so the next step no longer waits for the five-minute timer. A failed step does not wake, which keeps the timer's retry spacing. The scan records a `launched` event per step, and a suspected stall is stopped only after 300 seconds.
- Code jobs accept an optional `public_text` list of pull request units. The check fails a unit whose commit message has a subject that is not `type(scope): summary` or `type: summary`, a subject or line over 72 characters, no blank line after the subject, or a trailer line, and fails a missing `pr/<id>/title.txt` or `pr/<id>/body.md` in the job folder.
- One review covers a code job's diff and its public text. The reviewer reads a read-only snapshot of each unit's title, body and commit messages; a later text edit makes the PASS stale and starts a text-only round that shows the text diff. `taskwatch text <job> --out <dir>` writes the reviewed title and body with one `Review evidence:` line, and exits 2 when the text changed since the review, 3 when no review has passed.
- Review findings are one JSON line each (`finding`, `severity`, `category`, `rule`, `location`) with a `### F<n>` heading. After a failed review the worker records each finding in `findings.json` as `fixed` or `not_fixed`; a rule that repeats in an earlier round or in the last 10 jobs for the same target needs a `root-cause.md` section. `state/findings.jsonl` indexes the findings.
- The delivery message lists every P0, P1 and P2 finding, one line per rule with a count and the places, and one `Public text` and `Text fingerprint` line per unit. P3 findings stay in the review record.
- A spec for a `code` or `design` job follows `docs/spec-template.md`: submit refuses a missing or empty heading, and a bare `n/a`; `n/a: <reason>` passes. `source` and review-only jobs skip it.
- The reviewer of a `code` or `design` job answers eight design questions in the `design_check` of the verdict line, and the review message carries the approval source and scope. Guidelines: an owner decision is outside review, a writer's choice in the spec is not (C-1, K-4, C-18); K-19 compares writes outside the workspace with the approval scope.
- Code jobs start from a freshly fetched `origin/<default branch>` on a named branch (`<branch_prefix><id8>`, default `taskwatch/`), record `base`, `base_note`, `branch`, `workspace_state` and `branch_state`, and accept an optional `base` field for stacked work. The scan removes a clean worktree at `done` and `cancelled`, keeps a dirty one with the reason, and deletes the branch only once it is merged or squash merged into `origin/<default branch>`.
- Optional `env` object in `jobs-config.json` sets the environment of every process a job starts (omp worker, reviewer, check command, runner git commands); `PATH` entries expand `~`, `$HOME` and `$PATH`, so jobs find tools in `~/.local/bin` without per-job approval.
- A job keeps its checklist and progress files in its job folder instead of the worktree, and the worker prompt gives their absolute paths, so records are never committed into a result and the clean-tree check needs no exception. Records that name `.taskwatch/...` still load.
- `taskwatch watch <job>` blocks until a job reaches `approval_wait`, `user_wait`, `deliver_pending`, `done`, or `cancelled`, prints a short report, and exits (0 attention, 3 time limit, 2 unknown job). It reads `jobs.jsonl` without a lock or write and parses only new lines per poll, so a Hermes background process can wake the room that started the work.
- Optional `review_command` selects a separate executor command for independent job reviews, falling back to `omp_command` when omitted.
- Review-only jobs: submit a `review_target` (files or a commit) to review it without a work stage; the parsed verdict is recorded and delivered.
- `python -m taskwatch.reviewer_launch` builds the omp reviewer command at run time from an omp agent definition and the `docs/review` guidelines, so a review started by taskwatch or by omp alone reads the same guideline text; `review_command` accepts `{guidelines}` and `{review}`.
- Standalone approved job submission, parallel timer scheduling, transient omp RPC runs, interruption recovery, command checks, capped independent review fixes, and configured result delivery from `deliver_pending` to `done` through a literal-argument command with a fixed 60 second timeout. Only confirmed sends finish; every other outcome stops for user judgment without automatic resend. The existing Hermes plugin remains available.
- `taskwatch_approve` records conversational start and apply approvals without hand-written JSON; retries return the existing approval and apply refuses a stale or dirty result.
- `scan` warns through the configured user destination when the board is unreadable and exits 1; config failures exit 1 with the file and cause on stderr for the timer journal.
- Project skeleton: documentation, CI, and issue and pull request templates.
- Review guidelines for code, design, and source-checked documents, with a slot for project rules.
- CI checks that review guidelines keep required sections, local references, and unique rule ids.
- 13 review cases and a scorer that counts missed findings, false positives, severity mismatches, and verdict agreement from a human mapping.
- Hermes plugin entry that pins the tested Hermes version.
- `hermes taskwatch init`, `doctor`, `scan`, and `check`. The timer runs `scan` with the configured profile's `HERMES_HOME`, and `scan` takes the profile from the running Hermes, because Hermes consumes a `--profile` argument itself.
- `scan` sends the user line through Hermes' send path. A line is recorded only after it was delivered, and an unanswered or unsubscribed warning is checked on every scan.
- The checks read a card's exit code, outputs, fingerprint, basis, and reject count from run records and the live result, not from the card body.
- Optional `watch_hook` scan on the Kanban dispatch tick, off unless set.
- The gate blocks `kanban_complete` and `kanban_request_review` when a check fails. It approves only a result whose fingerprint, basis, and clean state match the recorded PASS as they are now, and it freezes exactly the fingerprint it checked.
- `examples/worker/run_executor.py` runs the executor in the foreground, records the run, and reads the review verdict from the review output. All paths in the record are absolute. A card behind an approved gate runs on its own work copy made from the approved snapshot, and is blocked when the gate has no snapshot. A review needs an output written by that run.
- `doctor` fails below the tested Hermes version and warns, without failing, above it.
- `scan`, `check`, the watch hook, the gate, and the card tools use the installed Hermes Kanban board.
- A passing gate approval writes the snapshot path onto the card.
- `taskwatch_create` and `taskwatch_reject` create cards and reject a gate. Create and reject run under the state lock, a failed create archives the children before their parents, and a reject writes its id and the card's last event id before it reopens so a retry finishes a half-done reject. The reject cap counts finished rejects, and a new reject id is refused while another is pending. A review-gate reject is refused, because Hermes has no public way to reopen the fixed worker card together with the review.
- `check --apply` and the apply examples stop when the fingerprint differs. The copy example publishes only a copy it hashed itself.
- Example skill telling a worker to run the executor in the foreground, to hand the verdict line to the review output, and to let the next card run on the approved snapshot.
- README install, update, and removal order, and docs for every config key, every check (what it blocks and when it can be removed), and the seven principles with their sources. Document approval trust boundaries, scan read failures, and known operational limitations.

### Changed

- Code jobs that build a result no longer wait for each other on one target, since each has its own branch and worktree; the slot limit still applies, and non-code and review-only jobs on one target still wait.
- Docs describe the standalone job runner only: README, `docs/checks.md`, `docs/configuration.md`, principles, contributing notes, and the package description no longer mention the removed plugin, cards, or gate, or call the project experimental.
- The plugin no longer changes environment variables, reads Hermes source, or writes the board database directly; profile and paths come from the plugin context and Hermes helpers.
- Generated timer units pin the configured profile with `-p`, and the apply example requires `HERMES_PROFILE`.
- `plugin.yaml` declares `taskwatch_approve`.
- `doctor` no longer checks review-gate reopen support or fails solely because that support is unavailable.
- The job field `kind` is now `review` with values `code`, `design`, and `source` (`document` and `research` merged into `source`), the reviewer launcher flag `--kind` is `--review`, the `review_command` placeholder `{kind}` is `{review}`, and `docs/review/source-checked-document.md` is `docs/review/source.md`; old `jobs.jsonl` records with `kind` still load.

### Removed

- The Hermes Kanban plugin: the plugin entry, cards, completion gate, Hermes tools, the `hermes taskwatch` command, their examples, and the shared-module code only they used. The `taskwatch` job runner is unchanged.

### Fixed

- A job with `approval_required` and `predecessors` asked the owner before its predecessors finished. It now stays queued until every predecessor is `done`.
- Verify declared output contents, including Git-ignored files, against PASS before delivery, and send stop notices without holding the global state lock.
- Send one stop notice per wait entry and recheck accepted PASS fingerprints before result delivery.
- Clarify safe plugin updates, default-profile timer pinning, production destinations, review executor responsibility, and user escalation; consolidate operational guidance.
- The enabled dispatch watch hook reports unreadable board evidence through the same user notification path as `scan`, without leaking the read error.
- P0 is only a rule break about runtime behavior or data. Style and wording are P2.
- The guideline check follows angle-bracket and reference-style local links.
- The scorer fails when an answers or mappings directory is missing or has no cases.

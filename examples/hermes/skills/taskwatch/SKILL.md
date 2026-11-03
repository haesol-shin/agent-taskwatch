---
name: taskwatch
description: Submit approved background work to taskwatch, wait for stops with `taskwatch needs --wait`, handle gates and user waits, and relay finished cards. Use when a request needs background research, design, implementation, or independent review.
version: 0.1.0
---

# taskwatch

Use `taskwatch` for approved background jobs (`review`: `source`, `design`, or `code`, or a `review_target`) that need deterministic checks and an independent review. Nothing installs this example skill automatically; copy it into your Hermes skills directory and adjust `<root>` if needed.

## 1. Split and prepare

- Split a multi-unit or multi-job request and write the `## Units` table by [`docs/review/design.md`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/review/design.md#splitting-a-plan) and [`docs/spec-template.md`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/spec-template.md); each independently reviewable and mergeable pull request is a separate job.
- Check `<root>/taskwatch/defaults.json` for standing owner choices ([`docs/configuration.md#owner-defaults`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/configuration.md#owner-defaults) and [`docs/kinds/design.md#choicesjson`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/kinds/design.md#choicesjson)).

## 2. Submit

Follow [`docs/configuration.md#chains-and-gates`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/configuration.md#chains-and-gates):

1. Read the current `cursor` with `taskwatch --root <root> needs` before or right after submitting.
2. Submit ordered jobs in one pass with `taskwatch --root <root> submit <job.json>`, giving each job `receipt_key: "<approval-source>:<n>"`, `predecessors` with the earlier job's receipt key (or id), and `base: "@<predecessor-receipt-key>"` for stacked `code` jobs.
3. Set `approval_required: true` on any job that waits for an owner decision on its predecessor's result.
4. For publication text, follow [`docs/configuration.md#public-text`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/configuration.md#public-text) for the one complete-result item and three required texts, including its body-only squash text; [`docs/checks.md#public-text-evidence`](https://github.com/haesol-shin/agent-taskwatch/blob/main/docs/checks.md#public-text-evidence) owns generated evidence, reviewed copies and historical nonlandable status.

## 3. Arm `needs --wait`

Start one background waiter for the room with `terminal(background=true, notify_on_complete=true)`:

```sh
taskwatch --root <root> needs --wait --from <cursor>
```

## 4. On wake

Always re-read state with `taskwatch --root <root> show <id>` before acting (`activity` shows the stage elapsed time, last progress age, and last progress line while a job runs).

- **Gate (`approval_wait`)**: show the owner `taskwatch --root <root> brief <predecessor>`, then pass the owner's reply to `taskwatch --root <root> approve <id> --reason "<reply>"` (prepend `PICKS: ok` or `PICKS: <id>=<key|text> ...` when answering design picks).
- **Needs you (`user_wait`)**: inspect `taskwatch --root <root> brief <id>`, ask the owner for a decision when required, then run `taskwatch --root <root> resume <id> --reason "<reply>"` or `taskwatch --root <root> cancel <id> --reason "<reply>"`.
- **Finished (`done`, `deliver_pending`)**: relay the delivery card (`delivery.txt`) unchanged. Give full details, paths, or findings only when the owner asks, via `taskwatch --root <root> brief <id>`. Export reviewed pull request text with `taskwatch --root <root> text <id> --out <dir>`.
- If jobs are still running after handling the event (or after exit code `3`), arm a new `needs --wait --from <new-cursor>`.

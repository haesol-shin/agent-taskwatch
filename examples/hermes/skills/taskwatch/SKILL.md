---
name: taskwatch
description: Submit approved background work to taskwatch, wait for stops with `taskwatch needs --wait`, handle gates and user waits, and relay finished cards. Use when a request needs background research, design, implementation, or independent review.
version: 0.1.0
---

# taskwatch

Use `taskwatch` for approved background jobs (`review`: `source`, `design`, or `code`, or a `review_target`) that need deterministic checks and an independent review. Nothing installs this example skill automatically; copy it into your Hermes skills directory and adjust `<root>` if needed.

## 1. Split and prepare

- Split a multi-unit or multi-job request and write the `## Units` table by [`docs/review/design.md`](../../../../docs/review/design.md#splitting-a-plan) and [`docs/spec-template.md`](../../../../docs/spec-template.md).
- Check `<root>/taskwatch/defaults.json` for standing owner choices ([`docs/configuration.md#owner-defaults`](../../../../docs/configuration.md#owner-defaults)).

## 2. Submit

Follow [`docs/configuration.md#chains-and-gates`](../../../../docs/configuration.md#chains-and-gates):

1. Read the current `cursor` with `taskwatch --root <root> needs` before or right after submitting.
2. Submit ordered units in one pass with `taskwatch --root <root> submit <job.json>`, giving each unit `receipt_key: "<approval-source>:<n>"`, `predecessors` with the earlier unit's receipt key (or id), and `base: "@<predecessor-receipt-key>"` for stacked `code` units.
3. Set `approval_required: true` on any unit that waits for an owner decision on its predecessor's result.

## 3. Arm `needs --wait`

Start one background waiter for the room with `terminal(background=true, notify_on_complete=true)`:

```sh
taskwatch --root <root> needs --wait --from <cursor>
```

## 4. On wake

Always re-read state with `taskwatch --root <root> show <id>` before acting (`activity` shows the stage elapsed time, last progress age, and last progress line while a job runs).

- **Gate (`approval_wait`)**: show the owner `taskwatch --root <root> brief <predecessor>`, then pass the owner's reply to `taskwatch --root <root> approve <id> --reason "<reply>"` (prepend `PICKS: ok` or `PICKS: <id>=<key|text> ...` when answering design picks).
- **Needs you (`user_wait`)**: inspect `taskwatch --root <root> brief <id>`, ask the owner for a decision when required, then run `resume`, `approve`, or `cancel`.
- **Finished (`done`, `deliver_pending`)**: relay the delivery card (`delivery.txt`) unchanged. Give full details, paths, or findings only when the owner asks, via `taskwatch --root <root> brief <id>`. Export reviewed pull request text with `taskwatch --root <root> text <id> --out <dir>`.
- If jobs are still running after handling the event (or after exit code `3`), arm a new `needs --wait --from <new-cursor>`.

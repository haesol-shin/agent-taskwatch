# Optional omp client

`examples/omp/taskwatch/` is an optional native [omp](https://omp.sh) extension that lets an omp session submit an existing approved job file, read a job's authoritative record and wait quietly for the owner's attention. It is a thin client of the installed `taskwatch` command: the CLI stays the only authority for approval assertions, receipts, job state and wake decisions, and nothing here runs a server, a poller or a model. taskwatch works the same without it.

## Requirements

- An installed `taskwatch` executable on `PATH` and a Linux host, as in the [installation](configuration.md#installation).
- An installed omp that loads TypeScript extensions. The client is written against the public `ExtensionAPI` and was tested with omp 18.14.0 only; no other version is claimed.
- Nothing else at run time. Bun, TypeScript and the pinned omp package are development dependencies of `examples/omp/taskwatch/` only, tested with Bun 1.4.2 and TypeScript 5.9.3. The Python package neither depends on nor ships them.

## Start

Start omp with the extension and one explicit taskwatch root:

```sh
omp -e <extension-path>/index.ts --taskwatch-root /srv/taskwatch-example
```

`--taskwatch-root` is the only configuration. It must be a nonempty absolute path. A missing or relative value is a visible error before any process starts; the client never falls back to the working directory, `HERMES_HOME`, a default root or any other path. Every call runs `taskwatch --root <root> <command> ...` as an argument vector without a shell, so paths with spaces or shell characters are passed unchanged. The executable is always `taskwatch` from `PATH`; there is no executable, model or provider setting, and a missing executable is reported as such.

The extension runs with your permissions. The root choice and a job file's approval assertion are not a sandbox, and the client never creates, edits, approves, cancels, resumes or waits on a job.

## Tools

The three tools mirror the flat CLI commands, with no slash command and no cancel, approve or resume tool.

| Tool | Input | Runs | Result |
| --- | --- | --- | --- |
| `taskwatch_submit` | `job_file`: existing absolute JSON file | `submit <job_file>` | The original job record, only if the CLI exits 0 with one JSON record that has a nonempty `id` and the same `receipt_key` as the file |
| `taskwatch_show` | `job_id`: nonempty string | `show <job_id>` | The parsed record; one JSON object whose `id` is the requested id |
| `taskwatch_needs` | optional `cursor`: opaque decimal string, `wait`: boolean (default `false`), `timeout_seconds`: integer 1 to 300 (default 60) | `needs [--from <cursor>]`, or `needs --wait --from <cursor> --timeout <seconds>` | The CLI's report and its `cursor` |

Non-wait calls have a fixed 60 second client deadline. A wait requires a cursor and has a deadline of `timeout_seconds` plus 5 seconds. The client never passes `--include-done`, so the CLI's [quiet wake policy](configuration.md#what-needs-the-owner) decides what ends a wait; a clean completion alone does not. The needs report is a hint: read the job with `taskwatch_show` before acting on it.

Every result is the standard tool `content` plus typed `details`. The details always carry `tool` and `state` (`ok`, `error`, `timeout`, `cancelled` or `unknown`), a `cause` for the last three, the complete `job_id`, `receipt_key` and `cursor` when they exist, the full parsed `record` or `report`, and the captured process `stdout`, `stderr`, `code` and `killed`. The displayed text is capped at 8 KiB of UTF-8, cut at a character boundary, with a notice that points to the details. The full record, report and identities are never shortened, and the whole CLI output is buffered in memory before it is returned.

## Cursors and waits

A cursor is an opaque decimal string and may exceed 2^53. The client keeps its characters and never converts it to a number. From a report it reads only the single `cursor: <digits>` line; a report with no such line, more than one, or one with other characters is an error. It does not read stage, verdict, approval or urgency from the text.

A wait that ends by its bound is an ordinary `timeout` result (CLI exit 3) that keeps the cursor you passed; re-arm explicitly with it. A wait that returns a report gives a new cursor for the next call. Exit codes 1 and 2 are errors. Nothing re-arms a wait or polls in the background.

A session owns at most one wait. Starting another aborts the old one and waits for its process to end before launching, so the old call returns `cancelled` and only one wait process exists.

## Interrupted submits

A submit that is cancelled, times out, loses its reply or returns something unreadable can still have been stored. The client then reports `unknown` with the `receipt_key` from the file and never an id, a rollback or a claim that a job was cancelled. To reconcile, call `taskwatch_submit` again with the same file and the same `receipt_key`: the CLI's [receipt replay](configuration.md#submit-an-approved-job) returns the original record whether or not the first call committed. A replay that returns a different receipt is an error. The client keeps no journal and never resubmits by itself, so keep the receipt and the file until the job is confirmed, including across a session restart.

## Cancellation and sessions

An abort signal, `session_switch` and `session_shutdown` end only the client's processes. They never run `taskwatch cancel` or change a durable job, and a submitted job survives. On a session change the client aborts every call of the old session (submit, show, needs and wait), waits for their processes to end, removes its listeners and fences the results: a late success is dropped and the call reports `cancelled` with `cause` `session` (`unknown` for a submit, with its receipt). A new session inherits no live call. The client never sends a message or starts a turn.

## Development

Run these in `examples/omp/taskwatch/`. They need Bun and, for the smoke, an installed `taskwatch`.

```sh
bun install --frozen-lockfile
bun run typecheck
bun run test
bun run smoke
```

`typecheck` is `tsc --noEmit` against the pinned public omp types. `test` runs the adapter tests, which use a typed fixture of the public API and a scripted process runner for argument vectors, root validation, receipt replay, replies, deadlines, abort and listener disposal, wait replacement, lifecycle fencing and text bounds. They do not replace the smoke.

`smoke` loads `index.ts` through the package's public `loadExtensions`, initializes the public `ExtensionRunner` with synthetic actions and runs the registered tools with the loader's own `pi.exec` against the installed `taskwatch`, in a throwaway directory with its own home, omp profile and root. It submits a synthetic approved job, replays it, shows it, takes a real cursor, runs a one second wait, loses a submit's reply after the durable commit and reconciles it, aborts and replaces waits, and checks shutdown leaves no wait process. A `systemd-run` stub keeps the submit wake from scheduling a real service, and a wrapper around the real CLI only hangs a submit after it was stored. It fails, never skips, when a prerequisite is missing. It makes no model call and writes nothing outside its directory.

Not exercised by this smoke: a model-driven omp session calling the tools, and the `omp -e ... --taskwatch-root ...` startup itself, which needs a configured default model.

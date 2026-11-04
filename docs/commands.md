# Commands

This page owns command usage: the spelling, arguments, options and exit codes of each `taskwatch` command. The command names and flags are flat on purpose; the groups below are only a reading order. [Configuration](configuration.md) owns storage, lifecycle, installation and recovery, [checks](checks.md) owns what a result must satisfy, and [docs/jobs](jobs/brief.md) owns what each job type must produce.

Every command takes the global `--root <root>` before the command name. [Configuration](configuration.md) explains how the root is selected when `--root` is omitted. `taskwatch --help` lists the commands and `taskwatch <command> --help` lists the options of one.

Unless a command says otherwise, it exits `0` on success and `1` when an error is printed as `taskwatch: <message>` on standard error. Commands that print a record print one JSON object (or a list of them) on standard output.

| Group | Commands |
|---|---|
| First use: submit and inspect | [`init`](#init), [`submit`](#submit), [`show`](#show), [`brief`](#brief) |
| Attention and decisions | [`needs`](#needs), [`cancel`](#cancel-wait-approve-and-resume), [`wait`](#cancel-wait-approve-and-resume), [`approve`](#cancel-wait-approve-and-resume), [`resume`](#cancel-wait-approve-and-resume) |
| Result checking and publication text | [`check`](#check), [`text`](#text) |
| Operator scheduling | [`scan`](#scan), [`run`](#run) |

## First use: submit and inspect

### init

`taskwatch --root <root> init [--unit-dir <dir>]` writes `<root>/taskwatch/jobs-config.json` when it is absent and validates it. With `--unit-dir` it also writes `agent-taskwatch-scan.service` and `.timer` into that folder and refuses a file that already exists. It installs, reloads and enables nothing. The output is a JSON object with the config path, whether units were written and `"activated": false`. See [Installation](configuration.md#installation) for the full steps.

### submit

`taskwatch --root <root> submit <job.json>` durably stores an approved job document and prints the stored record. The job is queued only after the document and its copied inputs are durable. A repeated `receipt_key` returns the original record. The fields are in [Submit an approved job](configuration.md#submit-an-approved-job). The command records the caller's approval assertion and cannot authenticate it.

After a successful submit, the command asks the user systemd manager to run one scan about two seconds later. A failed wake prints `taskwatch: warning: wake failed: ...` and never changes the exit code; the timer still scans. `approve`, `resume` and `run` wake the same way.

### show

`taskwatch --root <root> show [<job>]` prints the authoritative record of one job, or a list of every record. Each record carries one derived key, `activity`, which is not persisted: for a job in `work`, `fix`, `check`, `review` or `land` it reads `<stage> <m>m; last progress <m>m ago: <last non-empty progress line>`, with the progress text collapsed to at most 120 characters and omitted when empty, and for every other stage it is `null`. An unknown job id is an error, exit `1`.

Read `show` before acting on a `needs` list: the list is a hint, the record is the state. A stopped landing job exposes `land_state` here; inspect it before resuming or cancelling.

### brief

`taskwatch --root <root> brief <job>` prints the decision brief of a job, composed on every call and read only. It holds the runner's facts (job type, verdict, review rounds, check result, result location, changed files, open finding counts and time), the worker's sections, the picks with their reply line, what the reviewer checked and did not check, and at the end the command for the brief, the review body file (once delivery wrote it), the `Public text` folder and `Text fingerprint` of each unit, and the path of the worker's `brief.md`. The runner writes the facts from the job record; why, what changed, decisions and pointers come from the worker's `brief.md`, whose contract is [docs/jobs/brief.md](jobs/brief.md).

Exit codes: `0` printed, `2` unknown job, `1` another error. A job that has not written `brief.md` yet still prints the runner's facts, with `(none)` for the worker's sections.

## Attention and decisions

### needs

`taskwatch --root <root> needs [--from <cursor>] [--wait [--include-done]] [--timeout <seconds>] [--interval <seconds>]` lists what waits for the owner and prints an opaque integer `cursor`. Read only: no lock is taken and nothing is written.

- `--from <cursor>` takes the cursor of an earlier list unchanged and adds the jobs that finished since it. Do not calculate a cursor.
- `--wait` blocks until something new needs the owner and requires `--from`. `--include-done` also wakes on every new completion and is valid only with `--wait`.
- `--timeout <seconds>` is the longest wait, default `82800`, at least `1`. `--interval <seconds>` is the poll period, default `30`, at least `5`.

Exit codes: `0` a list and cursor were printed, `3` the timeout passed without a wake, `2` a usage error (`--wait` without `--from`, or `--include-done` without `--wait`), `1` another error. A timeout returns no cursor: re-arm with the original one. After exit `0`, handle the list and re-arm with the cursor it returned. Keep one watcher per room. The sections, wake conditions and cursor semantics are in [What needs the owner](configuration.md#what-needs-the-owner).

### cancel, wait, approve and resume

Each of these four takes the job id and a required `--reason <text>`, rewrites the job record under the state lock and prints the new record. The reason is appended to the job's events; `approve` and `resume` also number the owner entry that `needs` uses to detect a repeated stop.

- `taskwatch --root <root> cancel <job> --reason <text>` ends the job.
- `taskwatch --root <root> wait <job> --reason <text>` parks an unfinished job (`queued`, `work`, `check`, `review`, `fix` or `land`) in `approval_wait` until the owner decides. Any other stage is refused.
- `taskwatch --root <root> approve <job> --reason <text> [--scope <text>]` opens a job that is in `approval_wait` and appends the reason to its approval source. `--scope` replaces the approved scope with that exact text.
- `taskwatch --root <root> resume <job> --reason <text>` restores a job that is in `user_wait`: it opens a new review cycle at the saved stage and keeps every record. After a root-cause stop it needs a changed, complete note; see [finding records and repeated rules](checks.md#finding-records-and-repeated-rules).

A stage the command refuses is an error, exit `1`, and the record is unchanged. `approve` and `resume` wake a scan; `cancel` and `wait` do not. The choices behind each wait are in [Recovery and decisions](configuration.md#recovery-and-decisions) and [Chains and gates](configuration.md#chains-and-gates).

## Result checking and publication text

### check

`taskwatch --root <root> check <job> [--run-command]` prints, one `- ` line each, what the check step would reject about the shape of the job's result now: required files, progress and checklist, the brief, the public text files and commit messages, the finding records and uncommitted changes. It calls the same functions as the check step, so there is no second rule; [checks](checks.md) owns what is accepted. It reads the result and the records only.

`--run-command` also runs the job's `check_command` and writes its output to `logs/precheck.log` in the job folder. A run that exits 0 on a committed result that the command left unchanged is recorded in `logs/precheck.json`, and the check step reuses it instead of running the command again; a result that changed afterwards runs the command again, see [job checks](checks.md#job-checks). Without the option the command is not run.

Exit codes: `0` no problem (it prints `no problems`), `1` problems printed, `2` an unknown job, a review-only job, or a job that has no workspace yet.

### text

`taskwatch --root <root> text <job> --out <dir> [--pr <id>]` copies the reviewed `title.txt`, `body.md` and `commit.txt` into `<dir>`. `--pr` names the publication unit and may be omitted when the job has one. The command validates the accepted snapshot and the current text fingerprint first; the evidence and copy contract is in [public text evidence](checks.md#public-text-evidence). It publishes, pushes and merges nothing.

Exit codes: `0` the copy was written, `2` the live text or the accepted snapshot changed since the review, `3` no text review has passed, `1` another error, such as an unknown job or a job without `public_text`. A refusal prints its reason on standard error and writes nothing.

## Operator scheduling

### scan

`taskwatch --root <root> scan` schedules job steps once and prints the ids of the jobs whose step it launched as a JSON list. It is the timer's entry point and also the wake after `submit`, `approve`, `resume` and `run`. See [Timer entry point](configuration.md#timer-entry-point).

### run

`taskwatch --root <root> run <job> --attempt <n>` runs one reserved job step. `scan` starts it as a transient user service; do not run it alongside that service. It prints nothing on success; a step that ends with a failure code exits `1`.

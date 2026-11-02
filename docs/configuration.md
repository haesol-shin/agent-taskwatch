# Configuration and state

The `taskwatch` command never imports Hermes. `--root <root>` selects the root
explicitly; otherwise `HERMES_HOME`, then `~/.hermes`, is used. Select the shared
root, not a worker profile root. All state lives under `<root>/taskwatch/`; records
share the lock `<root>/taskwatch/state/lock`.
Use persistent, nonsynchronized storage for operational state. Temporary
directories are suitable only for synthetic tests.

## Job config

`taskwatch --root <root> init` writes `taskwatch/jobs-config.json` if absent:

```json
{
  "version": 1,
  "slots": 2,
  "stall_seconds": 1800,
  "omp_command": ["omp"],
  "rpc_timeout": 3600,
  "deliver_command": ["false", "{message}"]
}
```

All keys shown above are required. The three numeric settings are positive integer counts
or seconds. `omp_command` is an argument list prefix. Checks use the submission's
`check_command`, not this model command. `deliver_command` is described in
[Delivery](#delivery). The draft value
`["false", "{message}"]` is intentionally disabled: it can never send, so edit it before use.

Optionally add `"review_command": ["executor", "--role", "reviewer"]` to run
independent reviews with a different command prefix. Like `omp_command`, it must
be a nonempty list of nonempty strings. When omitted, reviews use `omp_command`;
work and fix always use `omp_command`. With the default `review_mode` (`"rpc"`), both commands receive `--mode rpc --no-ui`.
The `init` draft omits this optional key.

In `review_command` (and only there), an argument that is exactly `{guidelines}` becomes the
job's own copy of the review guidelines, and exactly `{review}` becomes the job's `review`
value. That lets the review command be the [reviewer launcher](review-launch.md), which
reads the agent definition and those guidelines when the review starts:

```json
"review_command": ["python3", "-m", "taskwatch.reviewer_launch", "--agent", "/srv/example/reviewer.md",
                   "--guidelines", "{guidelines}", "--review", "{review}"]
```

### Reviewer other than omp

Set `"review_mode": "print"` to review with a tool that is not omp (Codex, Claude Code, Gemini CLI, or any program). It needs `review_command` and adds no argument to it. The default is `"rpc"`; a host that sets nothing sees no change. Work and fix still run on `omp_command`.

In print mode the command runs once per review, in the job's folder, with the review message on its standard input. Besides `{guidelines}` and `{review}`, an argument that is exactly `{schema}` becomes the path of the verdict JSON Schema file (a copy of [`docs/launch/review-verdict.schema.json`](launch/review-verdict.schema.json) in the job folder) and exactly `{output}` the path of a file where the tool may write its last message. The reply must be one JSON object that follows the schema; [`docs/launch/taskwatch-print.md`](launch/taskwatch-print.md) is the text the reviewer reads. The schema has the keys the verdict line and the finding lines have in an RPC review, so the same rules decide what is accepted.

taskwatch looks for the verdict in the `{output}` file first, then in the standard output. In each it accepts the object itself (plain, or in a code fence), the object inside a wrapper (`structured_output` or `result` of `claude -p --output-format json`, `response` of `gemini -p --output-format json`), or the last line of a JSON-lines stream that holds it. It validates the object against the schema and fails the round with the schema path of the first errors (`$.findings[0].severity: 'P9' is not one of ...`) when it does not match; the raw reply is kept as the review text. There is no session, so there is no same-session repair: a malformed reply costs a round. A command that exits with a nonzero code or runs longer than `rpc_timeout` seconds fails the step with that reason.

taskwatch cannot restrict the tools of the other command; see [the limits](review-launch.md#print-mode-limits). Give each tool its read-only flags, as below. These examples were written on 2026-11-02 from the tools' own help or official pages; only `codex` is installed on the host that wrote them, so the `claude` and `gemini` lines are not run here.

Codex ([non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and `codex exec --help` of version 0.160.0): `--sandbox read-only` (read-only commands), `--output-schema` (the schema file), `-o` (the last-message file), `-` (read the prompt from stdin), `--ephemeral` (no session files), `--color never`. Add `--skip-git-repo-check` for a target that is not a Git repository.

```json
{
  "review_mode": "print",
  "review_command": ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--color", "never",
                     "--output-schema", "{schema}", "-o", "{output}", "-"]
}
```

Claude Code ([headless](https://code.claude.com/docs/en/headless) and [CLI reference](https://code.claude.com/docs/en/cli-reference)): `-p` reads the prompt from stdin; `--json-schema` takes the schema text, not a file, so a shell reads the file; `--permission-mode plan` and `--allowedTools` limit it to reading; `--output-format json` puts the validated object in `structured_output`. With `--bare` it skips hooks, plugins and `CLAUDE.md` but needs `ANTHROPIC_API_KEY`.

```json
{
  "review_mode": "print",
  "review_command": ["sh", "-c",
                     "claude -p --permission-mode plan --allowedTools \"Read,Grep,Glob,Bash(git diff *),Bash(git log *),Bash(git show *)\" --output-format json --json-schema \"$(cat \"$1\")\"",
                     "sh", "{schema}"]
}
```

Gemini CLI ([headless](https://geminicli.com/docs/cli/headless/) and [CLI reference](https://geminicli.com/docs/cli/cli-reference/)): `-p` appends its text to the standard input and forces non-interactive mode; `--approval-mode plan` is its read-only mode; `--output-format json` returns `{"response": "...", "stats": {...}}`. It has no schema flag, so the reply follows the schema because the message names the file; a reply that does not is a failed round.

```json
{
  "review_mode": "print",
  "review_command": ["gemini", "-p", "Review as the instructions on standard input say, and answer with the one JSON object they describe.",
                     "--approval-mode", "plan", "--output-format", "json"]
}
```

### Commit rules

Optionally add `"commit_rules"`, an object of booleans, to turn on commit message checks beyond the common ones in [checks](checks.md). Unknown keys are a config error. The `init` draft omits it, so a host that sets nothing sees no change.

- `bullet_one_line` (default `false`): a body bullet is one line. A bullet continued on an indented line fails the check of a pull request unit, with `line N continues the bullet on line M`.

```json
"commit_rules": {"bullet_one_line": true}
```

### Language

Optionally add `"language"`, `"en"` or `"ko"`, to choose the language of the card; the three headings of the delivery card are `Changed` / `Small problems left` / `Not checked`, or `바뀐 것` / `남은 작은 문제` / `확인하지 못한 것`. Any other value is a config error. The `init` draft omits it. The key also names the language of the card text: `WORKFLOW.md` tells the worker to write the lines of the brief's `Card` section in that language, and the review prompt tells the reviewer to write each finding `summary` and the items of `not_checked` in it. Without the key, the labels are English and the worker and the reviewer write in the language of the intent.

```json
"language": "ko"
```

### Job environment

Optionally add `"env"`, an object of string keys and string values, to set the environment of
every process the runner starts for a job: the omp worker (work, fix, review, including the
reviewer command), the check command, and the git commands the runner runs. A systemd job
starts with a minimal `PATH`, so tools such as `gitleaks` in `~/.local/bin` or `omp` plugins in
`~/.bun/bin` are missing for a commit hook until you add them here. Values are used as written,
except in `PATH`, where `~` and `$HOME` expand and `$PATH` stands for the inherited value. No
shell runs. Anything that is not an object of strings is a config error; omitting `env` or
leaving it empty keeps the inherited environment. The `init` draft omits this key.

```json
"env": {"PATH": "~/.local/bin:~/.bun/bin:$PATH"}
```

### Owner defaults

Optional `<root>/taskwatch/defaults.json` holds standing owner answers for design jobs (`{"version": 1, "defaults": [{"id", "value", "scope": "global" | "<target path>", "source": "<owner words>"}]}`), validated against [`docs/kinds/defaults.schema.json`](kinds/defaults.schema.json). When the same `id` has several applicable entries, the most specific scope wins (a target path beats `global`; a longer matching path beats a shorter one), as in `git config` and Claude Code settings. Every design job's work and review messages name the file and its short SHA-256 hash (`Owner defaults: <root>/taskwatch/defaults.json (sha256 <12 hex> | absent)`), and the `choices.json` check fails a malformed file, an unknown `defaults_applied` id, or a pick whose `id` is already settled by an applicable default; see [`docs/kinds/design.md`](kinds/design.md).

### Code job work folders

A `code` job that builds a result works in `state/jobs/<id>/worktree`, on its own branch.

- Base. Before the worktree is created, the runner runs `git fetch origin <default branch>`
  without prompting and with a 5 second cap. The default branch is the one `origin/HEAD`
  names, or `main`. The worktree starts at `origin/<default branch>`. If the fetch fails or the
  target has no `origin`, the local ref is used and the job records `base_note: "base may be
  stale: <reason>"`. The optional submission field `base` (a commit or branch of the target, or
  `@<receipt key>` for the branch `<branch_prefix><first 8 characters of the id>` of an existing
  code job) replaces the base, for work stacked on another job's branch; it is used as written,
  with no fetch, and `@<receipt key>` is stored as the resolved branch name. Afterwards `base`
  in the record is the commit used.
- Branch. `<branch_prefix><first 8 characters of the job id>`. Optionally add
  `"branch_prefix"` to `jobs-config.json` (default `"taskwatch/"`): letters, digits, `_` and
  `-`, with parts joined by `/`. A resumed job reuses its worktree and branch.
- Cleanup. When a job reaches `done` or `cancelled`, and not at `deliver_pending`, the scan runs
  `git worktree remove` without `--force`. A folder with uncommitted or untracked files, or
  that git refuses to remove, stays and `workspace_state` says why (`kept: <reason>`); the scan
  retries it. The job folder, records, reviews and logs are never deleted.
- Branch deletion. After a fetch, the branch is deleted only when it is integrated: its tip is
  in the history of `origin/<default branch>`, or merging it onto that branch changes nothing
  (a squash merge already holds its content). Otherwise `branch_state` stays `kept` and the
  scan checks again later. `integrated` means integrated but git refused the deletion. The
  integration check needs git 2.38 or later (`git merge-tree --write-tree`); on an older git
  only a branch in the history is deleted.
- Record fields: `base`, `base_note`, `branch`, `workspace_state` (`active`, `removed`,
  `kept: <reason>`) and `branch_state` (`kept`, `integrated`, `deleted`), shown by
  `taskwatch show`. Records from before these fields exist keep their detached worktree and are
  not cleaned up.

### Job records

A job keeps its checklist, progress and brief files in its job folder (`state/jobs/<id>/`), next to
the logs and reviews, not in the workspace. The worker prompt and `WORKFLOW.md` give their
absolute paths. The records are therefore never part of a result, and the clean-tree check of a
`code` job needs no exception. `WORKFLOW.md` also carries the brief contract and the rules of the
job's kind from `docs/kinds/`. A job submitted earlier whose paths name `.taskwatch/...` still
loads; those paths now resolve inside the job folder as well.

### Submit an approved job

Pass a JSON file to `taskwatch --root <root> submit <file>`. Example:

```json
{
  "receipt_key": "approval-message-1:job-1",
  "approval_source": "approval-message-1",
  "approval_scope": "Create local deliverables only",
  "prompt": "Write the approved deliverable and record execution evidence.",
  "review": "source",
  "target": "/srv/example-target",
  "intent": "/srv/example-inputs/intent.md",
  "spec": "/srv/example-inputs/spec.md",
  "inputs": {
    "work": ["/srv/example-inputs/context.md"],
    "review": ["/srv/example-inputs/review-context.md"]
  },
  "outputs": ["result.md"],
  "check_command": ["sh", "check-result.sh"],
  "checklist": "checklist.json",
  "progress": "progress.md",
  "predecessors": [],
  "expected_wait_seconds": 0,
  "approval_required": false
}
```

`target` must exist. `review` picks the review guidelines the reviewer reads: `code`,
`design`, or `source` (a document that cites sources, such as research notes, summaries,
or comparisons). Only `code` also runs the work in a Git worktree, because a code review
reads the Git diff.
A job with `review` `code` or `design` and a work contract needs a spec that follows
[docs/spec-template.md](spec-template.md): each of its thirteen `## ` headings is present and
answered, or says `n/a: <reason>`. Submit refuses a missing or empty heading by name, and a
bare `n/a`. A `source` job and a review-only job skip this check. For these two kinds the
reviewer's verdict line also carries a `design_check` of eight answers (`root_cause`,
`alternatives`, `one_home`, `overlaps`, `needed_now`, `repeated_judgment`, `portability`,
`choices`), each `ok`, `finding` or `na` with its evidence; the runner checks only its shape.
The review message names `approval_source` and `approval_scope`, so the reviewer can tell an
owner decision from a writer's choice and compare the writes of the change with the scope.
Output paths are relative to the job workspace, checklist and progress paths to the job
folder; none can traverse outside its base. `check_command` runs as argv without an implicit
shell; name a shell explicitly only when the approved check needs one.
The work must create that check script if it is not already in its workspace.
The checklist follows the existing nonempty `items` list of boolean `passes`.

`inputs`, `predecessors`, `expected_wait_seconds`, and `approval_required` are
optional. Every other example field is required. Each entry of `predecessors` is
an existing job's id or `receipt_key` (id first, else `receipt_key`); the stored
record keeps the resolved job ids. Derive the receipt key from the approval
message and its job number. A repeated key returns the original job even if other
arguments differ.
The command records the caller's approval assertion; it cannot authenticate
the conversation.

A job with `predecessors` gets one block per predecessor in the work and review
messages: its id, kind label (`research` for `review: source`, `design`,
`implement` for `review: code`, `review` for a `review_target`), stage, result
location (the output files in its workspace, or for a code job the branch
`<branch_prefix><first 8 of the id>` in its target), result fingerprint and the
path of its `brief.md`. The block is read only: nothing is copied, so input hashes
and the review basis do not change, and the spec still says how to use the result.

`approval_required` defaults to `false`. If `true`, the first scan moves the job
to `approval_wait` before its first work run. With `predecessors`, the job stays
`queued` (wait reason `predecessor:<id>`) until every predecessor is `done`, so the
owner has the predecessor's result before the question; a cancelled or
`user_wait` predecessor leaves it queued. A job without `approval_required`
still starts once its predecessors reach `deliver_pending`. `approve` clears the
flag and allows work to start. A later `wait` records the current step for approval.

`approval_scope` is a recorded boundary, not operating-system isolation.
The runner's shell tools have the user's permissions; instructions and
explicit approval records cannot enforce those permissions.

Code jobs that build a result accept an optional `public_text` list for the pull
requests the job produces. Each unit is `{"id": "pr1", "base": "<ref>", "head": "HEAD"}`;
`head` defaults to `HEAD`, and `id` is lowercase letters, digits and hyphens. For a
stacked series the `base` of a unit is the `head` of the one before. A `base` must
resolve in `target` at submit unless it is another unit's `head`; refs are resolved
again in the work folder at every check. The worker writes `pr/<id>/title.txt` and
`pr/<id>/body.md` in the job folder, never in the work folder. The check fails when a
file is missing, a unit has no commits, or a commit message breaks the common rules:
title is `type(scope): summary` or `type: summary` and at most 72 characters, one blank
line follows the title, no line is longer than 72 characters, no trailer line
(`Co-authored-by:`, `Signed-off-by:` and similar). There is no limit on bullets.
The review reads the same text, `taskwatch text <job> [--pr <id>] --out <dir>` hands out the
reviewed title and body, and the delivery message names each unit's folder and text
fingerprint; see [review records](checks.md#review-records).

The check command must exit `0`. Required outputs and a nonempty passing
checklist must also exist after the command finishes. Minimal checklist:

```json
{"items": [{"passes": true}]}
```

No other checklist item fields are required. A `false` item, missing file,
invalid JSON, or empty list blocks progress. The independent reviewer returns
exactly one verdict JSON line followed by findings, for example:

```text
{"verdict": "PASS", "blocking": false, "reviewer": "independent-review", "checked": "the diff against the spec", "not_checked": "runtime behavior"}
No blocking findings.
```

A FAIL, malformed verdict, missing input, or blocking finding prevents
`deliver_pending`. See [review records](checks.md#review-records) for the shared contract.

Submission copies and hashes intent, spec, and role-specific inputs. The
original prompt is kept intact. `jobs.jsonl` stores append-only, checksummed
snapshots. The existing torn-tail repair preserves a partial last line before
the next append. Corrupt committed records block scheduling rather than
letting a job start or pass. Inspect the scan service journal for diagnostics.

Each job has `state/jobs/<id>/`, a separate workspace, copied inputs, a
`WORKFLOW.md`, and execution evidence. Code jobs use a git worktree on their own
branch; see [Code job work folders](#code-job-work-folders). This command does not merge or
apply results.

#### Review-only jobs

Replace `outputs`, `check_command`, `checklist`, and `progress` with one `review_target` to
review something that already exists. There is no work stage and no check; the job starts at
the review step, in the target directory itself.

```json
{
  "receipt_key": "approval-message-1:review-1",
  "approval_source": "approval-message-1",
  "approval_scope": "Review only; change nothing",
  "prompt": "Review this change against the intent and the spec.",
  "review": "code",
  "target": "/srv/example-repo",
  "intent": "/srv/example-inputs/intent.md",
  "spec": "/srv/example-inputs/spec.md",
  "review_target": {"commit": "HEAD"}
}
```

`review_target` is exactly one of `{"files": ["relative/path"]}` (files under `target`) or
`{"commit": "<revision>"}` (resolved to a full SHA when the job is submitted). `inputs`,
`predecessors`, `expected_wait_seconds`, and `approval_required` work as for other jobs.

The verdict is the result. A parsed `PASS` or `FAIL` is recorded and delivered; a `FAIL` does
not start a fix, because there is nothing to fix. An answer with no acceptable verdict line, or a
target that changed during the review, costs a round and reviews again. The cap is the one other jobs have: the first review plus three
retries, four reviews in all; after that the job stops for user judgment (`review_exhausted`). The review record and
job state are in `show <id>`.

### Timer entry point

Install the scan timer with the units that `init` stages; see
[Installation](#installation). The timer is the fallback and keeps its five-minute
schedule. A scan also runs two seconds after a step ends and after `submit`,
`approve` and `resume`, through `systemd-run --user --on-active=2s`; a failed
wake only prints a warning. A failed step does not wake, so the timer spaces
its retries.

`taskwatch --root <root> init --unit-dir <empty-unit-staging-directory>` writes
`agent-taskwatch-scan.service` and `.timer` without installing, reloading, or
enabling them. Existing files are refused. The service calls the standalone
`scan` with the current interpreter and explicit root. Inspect the staged files
and replace the existing service only as part of an approved installation.
Run `systemctl --user daemon-reload` after installing it. Confirm the timer is
enabled and active, and confirm linger with `loginctl show-user <user> -p Linger`
if jobs must survive logout. Run these installation steps only on the intended
host with approval.

`scan` is a short scheduler, not a resident dispatcher. It reserves the job
attempt durably before invoking `systemd-run --user`. The deterministic
`taskwatch-<id>-a<attempt>.service` name allows a later scan to find a start
whose acknowledgement was lost. `taskwatch run <id> --attempt <n>` is an
internal step command; do not run it alongside the service.

### Installation

Do these steps in order on the intended host. Stop at the first failure.

1. If `<root>/taskwatch/jobs-config.json` already exists, add `deliver_command` to it before running `init` (see [Delivery](#delivery)); `init` validates existing config without changing it. Fresh installations can keep the init-then-edit order below.
2. Stage the units: `taskwatch --root <root> init --unit-dir <empty-unit-staging-directory>`. This also writes `jobs-config.json` if absent. Nothing is installed or enabled.
3. Edit `<root>/taskwatch/jobs-config.json`, including `deliver_command` (see [Delivery](#delivery)).
4. Inspect the staged `agent-taskwatch-scan.service` and `.timer`, install them into `~/.config/systemd/user/`, then run `systemctl --user daemon-reload`.
5. Enable the timer with `systemctl --user enable --now agent-taskwatch-scan.timer`. Confirm it is enabled and active, and confirm linger with `loginctl show-user <user> -p Linger` if jobs must survive logout.
6. Submit jobs with `taskwatch --root <root> submit <approved-job.json>`.

### Reinstall after the rename

The distribution is `agent-taskwatch` and the units are `agent-taskwatch-scan.service` and `.timer`. The command `taskwatch` and the import package `taskwatch` keep their names. On a host that runs the old units, with no job running:

1. Install the new distribution with `uv tool install --force <checkout>`; the old tool directory no longer matches the name.
2. Stop and disable the old timer: `systemctl --user disable --now hermes-taskwatch-scan.timer`, then remove the old `hermes-taskwatch-scan.*` files from `~/.config/systemd/user/`.
3. Stage new units with `taskwatch --root <root> init --unit-dir <empty-unit-staging-directory>`, install them, run `systemctl --user daemon-reload`, and enable `agent-taskwatch-scan.timer`.
4. Point `review_command[0]` in `jobs-config.json` at the new tool directory, then run `uv tool uninstall hermes-taskwatch`.

### Delivery

The `init` draft sets `deliver_command` to `["false", "{message}"]`, which
intentionally cannot send. An attempt with this draft stops for user judgment,
so replace it before enabling the timer.

`jobs-config.json` requires `deliver_command`: a list of nonempty strings run as
argv without a shell. Exactly one argument must be the whole string `{message}`;
it is replaced with the absolute path of the generated message file. The
destination is a literal argument written in this command, not the job's
`target` (a workspace path). Example, with a synthetic destination:

```json
"deliver_command": ["hermes", "send", "--to", "discord:#synthetic-channel", "--file", "{message}", "--json"]
```

Choose a configured Hermes target with `hermes send --list`.

Hermes is optional. Without it, provide a sender executable for your channel and
configure its argv instead, for example
`["notify-command", "--to", "person.invalid", "--file", "{message}"]`.
The sender must wait for the channel server's response and implement this contract:
stdout must contain exactly one JSON object, with no diagnostic text around it.
Write diagnostics to stderr. Both streams and the exit code are recorded.
For example, exit 0 with `{"success": true, "message_id": "synthetic-message"}`
confirms sending. Every other outcome is unknown, including a nonzero exit with
a readable JSON `error`: a timeout or partial send may already have delivered content.

For a job at `deliver_pending`, `scan` runs this step under the state lock after
the old unit has been released. It writes `delivery.txt` and `delivery-review.txt` in the
job folder, records the attempt as `started` with the message file and the SHA-256 of
`delivery.txt`, then runs the command with a fixed 60 second timeout. `delivery.txt` is
the card and nothing else: the conclusion from the worker's `## Card`, a blank line, then only the
lists that have content under their [`language`](#language) headings: the worker's bullets
(`Changed`), one bullet per P2 or P3 finding of the last review that has a `summary`, worst first (`Small
problems left`), and the items of the reviewer's `not_checked`, split on `; ` (`Not checked`).
A list shows at most 6 bullets of at most 160 characters; a seventh shows as `+N`. The message has
no paths and no pick block, so it stays under Telegram's 4096 characters.
`taskwatch --root <root> brief <job>` composes the full brief on read: the facts, the worker's
sections, the picks with their reply line, what the reviewer checked and did not check, and at
the end the command for the brief, the review body file (once delivery wrote it), the `Public
text` folder and `Text fingerprint` of each unit, and the path of the worker's `brief.md`. It is
read only and exits `2` for an unknown job. The facts are written by the runner from the job
record: kind, verdict, review rounds, check result, result location, changed files, open finding
counts and time. Why, what changed, decisions and pointers come from the worker's `brief.md`.

Before sending, the result fingerprint, declared output content digest, review
basis and the text fingerprint of each unit must match the accepted PASS. The output digest also covers Git-ignored files.
Changed, missing, or unreadable evidence returns to `check`, spending the existing
recovery budget; exhaustion stops at `user_wait/result_unstable`.

After scheduling work and result deliveries, each scan sends one short stop
notice for every new `user_wait` or `approval_wait` entry through the same
`deliver_command`, timeout, and success contract. `notice-<index>.txt` names the
job, first prompt line, stage, wait reason, error, and resume/approve/cancel
commands. Attempts are reserved in `notices` under the state lock before sending.
The sender runs without that lock; only its notice result is merged into the latest
snapshot, so unrelated jobs can finish while it waits. No outcome causes an automatic
retry or changes the stage. Interrupted attempts become unknown.
`delivery_unknown` receives a separate notice, never a result resend.

For result delivery, success needs exit code 0, a JSON object with a truthy
`success`, no `error`, and a present `message_id`. Then the job becomes `done` and is never sent again.
Success means the command reported acceptance; it is not a read receipt. A
response with only `skipped` is unknown, not done.

- Every non-confirmed outcome (including a readable `error`, timeout, unreadable
  output, signal, ambiguous response, or an earlier attempt still `started`)
  moves the job to `user_wait` with reason `delivery_unknown`. It is never resent
  automatically. Hermes provides neither lookup nor an idempotency key.
- `resume <id>` restores `deliver_pending` for one new send and keeps the attempt
  history; an interrupted `started` attempt is recorded as unknown. Resume only
  when a duplicate message is explicitly accepted, because the earlier message
  may have been delivered. `cancel` prevents further sends.

### Checking a result before it ends

`taskwatch --root <root> check <job-id> [--run-command]` prints, one `- ` line each, what the check step would reject about the shape of the job's result now: required files, progress and checklist, the brief, the public text files and commit messages, the finding records, and uncommitted changes. It calls the same functions as the check step, so there is no second rule. Exit codes: `0` no problem (it prints `no problems`), `1` problems printed, `2` unknown job, a review-only job, or a job that has no workspace yet. It reads the result and the records only. `--run-command` also runs the job's `check_command`, with its output in `logs/precheck.log`; without it the test command is not run, because the check step runs it anyway. `WORKFLOW.md` names the command in its End section as the last step before the agent finishes.

`WORKFLOW.md` tells the worker to run only the tests that cover the files it changed while working, and `check <job> --run-command` once just before it ends.

### Watching a job

`taskwatch --root <root> watch <job-id> [--timeout <seconds>] [--interval <seconds>]` blocks
until the job stage is `approval_wait`, `user_wait`, `deliver_pending`, `done`, or
`cancelled`, prints a report of at most 20 lines (id, stage, `wait_reason`, whether an `error` is recorded (its text is not printed), last
review verdict, output and detail paths, next commands), and exits. It exits at once when the
job already stopped. Exit codes: `0` needs attention, `3` the `--timeout` (default 82800)
passed with nothing to report, `2` usage error or unknown job, `1` other error. `--interval`
defaults to 30 and is at least 5. The command is read only: it takes no lock and writes
nothing. It reads `jobs.jsonl` once, then only the lines appended since the last poll, because
a full load of a large ledger takes about a second. Hermes starts it from the room that
submitted the job:

```python
terminal(background=True, notify_on_complete=True, command="taskwatch --root <root> watch <id>")
```

The report is a hint; re-read the state with `show` before acting. `approve` and `resume`
move the job to `queued`, so a watcher started after them waits for the next stop.

### Chains and gates

A kind is a label derived from `review` and `review_target`; it names the contract in `docs/kinds/` that `WORKFLOW.md` carries.

| Kind | Fields | Result | Checked by code |
|---|---|---|---|
| research | `review: source` | `result.md`, `claims.json`, `sources/` | source check (when `outputs` has `claims.json`), checklist, brief |
| design | `review: design` | `design.md`, `choices.json` | `choices.json`, `check_command`, checklist, brief |
| implement | `review: code` | committed branch | the repository's tests as `check_command`, clean commit, brief |
| review | `review_target` set | the review record | none (no work stage) |

Publishing is a gate, not a kind. The common chain is research, a gate, design, a gate, implement; a small change can skip the first two, and a design job with `claims.json` in `outputs` includes its own research.

The submit rule for a caller that sends ordered units under one approval:

1. Submit the units in order, each with the receipt key `<approval message>:<n>` and `predecessors` naming the unit before it by its receipt key (or its id). Because receipt keys are known before submit, the whole chain can be submitted in one pass; the stored record keeps the resolved ids. The message of the successor lists each predecessor's result, so the prompt needs no pasted path; for a stacked code unit set `base` to `@<predecessor receipt key>` (or `<branch_prefix><first 8 characters of its id>`). Units with no order between them are submitted together without `predecessors`.
2. A unit that needs the owner's decision on an earlier result (a design before its implementation) carries `approval_required: true`. It stays queued until every predecessor is `done`, then waits in `approval_wait` with the brief of the predecessor ready. Show the owner the output of `taskwatch brief <predecessor>`, then pass the owner's reply to `approve --reason`, preceded for picks by one line `PICKS: ok` or `PICKS: <id>=<key|text> ...`. The reply is appended to the approval source, so the worker and the reviewer see it as the owner's decision.
3. A unit whose `spec`, `intent` or `inputs` must be a predecessor's output cannot be submitted early: those files are copied and hashed at submit. Submit it after the output exists.
4. A cancelled or stopped predecessor leaves its successors queued; `needs` lists them as blocked. Cancel or resubmit them.
5. Arm one `needs --wait --from <cursor>` for the room instead of one `watch` per job.

### What needs the owner

`taskwatch --root <root> needs` lists what waits for the owner and prints a `cursor` (the
byte size of `jobs.jsonl` it read). It is read only: no lock, nothing written, derived on
every call. Sections: `GATE` (a job in `approval_wait`: with `predecessors` it names them and
their briefs and counts the decisions in their `brief.md`, or the picks of a design predecessor with the reply template `ok | <id>=<key> | text`; otherwise it shows the reason),
`NEEDS YOU` (a job in `user_wait`: reason, review round, open findings, rules that repeated
in earlier reviews), `BLOCKED` (a queued job behind a cancelled or `user_wait` predecessor)
and, only with `--from`, `FINISHED SINCE THE CURSOR` (a job that is `done` now and was not at
the cursor, with its verdict, decision count and open findings). At most 20 jobs are shown.

`needs --wait --from <cursor> [--timeout <seconds>] [--interval <seconds>]` blocks until the
list holds an item the cursor did not, then prints the whole list and a new cursor. An item
is `(job, stage, reason, entry)`, where `entry` counts the job's `wait`, `approve` and
`resume` events, so a job that is answered and then stops again for the same reason wakes
the watcher, and a re-save of the same stop does not. A cursor beyond the ledger lists
everything again. Exit codes: `0` listed, `3` the `--timeout` (default 82800) passed, `2`
usage error (`--wait` without `--from`), `1` other error such as a corrupt snapshot of a
listed job. One watcher per room replaces one `watch` per job:

```python
terminal(
    background=True,
    notify_on_complete=True,
    command="taskwatch --root <root> needs --wait --from <cursor>",
)
```

The list is a hint; re-read the state with `show` before acting. `taskwatch --root <root> show [<id>]` prints the job record (or the list of records) as JSON with one derived key, `"activity"` (not stored in `jobs.jsonl` and not in `needs`): for a job in `work`, `fix`, `check` or `review`, `"<stage> <m>m; last progress <m>m ago: <last non-empty progress line>"` (from `started_at`, `last_progress_at` and the progress file in the job folder, collapsed to one line of at most 120 characters, or `"<stage> <m>m; last progress <m>m ago"` when the progress file has no non-empty line yet); `null` for any other stage. An example Hermes skill for this loop is in [`examples/hermes/skills/taskwatch/SKILL.md`](../examples/hermes/skills/taskwatch/SKILL.md).

### Recovery and decisions

The job retains its parallel slot through work, checks, review, and fixes.
A job that works in a shared folder (a non-code job, or a review-only job) locks its target
across these stages; a code job that builds a result does not, because its worktree is its
own (see [Code job work folders](#code-job-work-folders)). A blocked predecessor leaves
the successor queued. An unrelated target can still start.
Dead or reboot-interrupted runs resume only after the old service is known
to have stopped. An unknown service outcome requires user judgment.
Unchanged progress and output content first produces a stall suspicion;
an unchanged scan at least 300 seconds later stops the service and recovers.
Log growth alone is not progress. Expected waits delay stall recovery.

When a run is found dead and the filesystem of the job folder or of `TMPDIR` (the configured
`env` value, else the system temporary folder) has less than 256 MiB free, the scan does not
relaunch. It parks the job as `user_wait` with `wait_reason: disk_full` and
`error: disk_full: <path> has <n> bytes free, the floor is <bytes>`, and uses no recovery. Free
space, then `resume` the job.

Three consecutive failed recoveries stop for user judgment. Passing checks
reset that recovery budget. The independent review has a separate per-cycle
cap of three automatic fix rounds. A fresh review uses `docs/review/` profiles
and the existing verdict JSON contract. Fingerprints and review basis are
computed by code; changing either invalidates the earlier pass.

```sh
taskwatch --root <root> wait <id> --reason "Unapproved external write"
taskwatch --root <root> approve <id> --reason "approval-message-2" --scope "Exact approved scope"
taskwatch --root <root> resume <id> --reason "Approved a new fix cycle"
taskwatch --root <root> cancel <id> --reason "User cancelled"
```

Wait and cancel fence late output immediately. The scanner confirms service
termination before reusing the slot or target. Approval resumes the saved step;
an explicit user resume opens a new recovery/fix cycle. Resuming a review fix
reserves its first round before launch. Check repairs keep the separate recovery
budget. Commands do not send messages themselves; scans send results and stop
notices. The runner does not reconcile an external outcome.

`approve --reason` appends `Gate <date>: <reason>` to the approval source; the
original approval stays and the worker and reviewer see both. `approve --scope`
records a replacement approved scope. Omit it to preserve the original scope,
such as an initial approval wait. Do not use a broader scope than the user
actually approved.

After checks and review pass, the stage is `deliver_pending`, not `done`; see
[Delivery](#delivery).

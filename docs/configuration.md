# Configuration and state

The `taskwatch` command never imports Hermes. `--root <root>` selects the root explicitly; otherwise `HERMES_HOME`, then `~/.hermes`, is used. Select the shared root, not a worker profile root. All state lives under `<root>/taskwatch/`; records share the lock `<root>/taskwatch/state/lock`. Use persistent, nonsynchronized storage for operational state. Temporary directories are suitable only for synthetic tests.

## Job config

`taskwatch --root <root> init` writes `taskwatch/jobs-config.json` if absent:

```json
{
  "version": 1,
  "slots": 2,
  "stall_seconds": 1800,
  "omp_command": ["omp"],
  "rpc_timeout": 3600,
  "deliver_command": ["false", "{message}"],
  "max_reviews": 3
}
```

All keys shown above except `max_reviews` are required. The three numeric settings are positive integer counts or seconds. `omp_command` is an argument list prefix. Checks use the submission's `check_command`, not this model command. `deliver_command` is described in [Delivery](#delivery). The draft value `["false", "{message}"]` is intentionally disabled: it can never send, so edit it before use.

`max_reviews` limits the reviews of one cycle; see [Review limit](#review-limit).

`install_command` is optional and defaults to `null`; [Automatic landing](#automatic-landing) owns its host-only argv contract, frozen identity and explicit retry rules.

Optionally add `"review_command": ["executor", "--role", "reviewer"]` to run independent reviews with a different command prefix. Like `omp_command`, it must be a nonempty list of nonempty strings. When omitted, RPC reviews use `omp_command`. With the default `review_mode` (`"rpc"`), review commands receive `--mode rpc --no-ui`. The `init` draft omits this optional key. Work and fix select their command through [Worker execution](#worker-execution).

In `review_command` (and only there), an argument that is exactly `{guidelines}` becomes the job's own copy of the review guidelines, and exactly `{review}` becomes the job's `review` value. That lets the review command be the [reviewer launcher](review-launch.md), which reads the agent definition and those guidelines when the review starts:

```json
"review_command": ["python3", "-m", "taskwatch.reviewer_launch", "--agent", "/srv/example/reviewer.md",
                   "--guidelines", "{guidelines}", "--review", "{review}"]
```

### Worker execution

`worker_mode` is optional, accepts exactly `"rpc"` or `"print"` and defaults to `"rpc"` when omitted. Explicit `"worker_mode": "rpc"` preserves the existing omp worker behavior. Optional `worker_command` must be a nonempty argv list of nonempty strings; print mode requires it. RPC work and fix use `omp_command`, never `worker_command`. `omp_command` remains a required nonempty argv list even when unused, but taskwatch does not resolve or launch it in a fully print configuration. No worker placeholder substitution, shell parsing, vendor flags or model/role reinterpretation occurs.

Print work and fix each launch the configured argv once in the existing job workspace with the [job environment](#job-environment). The exact worker message arrives as UTF-8 stdin followed by EOF: `WORKFLOW.md`, copied inputs, predecessor evidence, approval scope, progress/checklist/brief paths, fix findings and any unfinished-work reminder retain their existing meaning. Exit zero permits deterministic checking only. Stdout, including `PASS` text or reviewer-shaped JSON, is execution evidence, never a verdict. Checks, independent review and existing fingerprint/basis guards still decide acceptance.

The workspace is the per-job Git worktree for code jobs and the per-job `work` directory for source/design jobs; see [work folders](#code-job-work-folders) and [records](#job-records). For a first run, follow [submission](#submit-an-approved-job); reviewer placeholders and file/stdout reply priority are defined in [print review](#reviewer-other-than-omp).

Work/fix attempts durably record the selected mode, argv and executable resolution before launch under the existing attempt/stage guards. Historical attempts without print identity retain RPC meaning. Configuration changes cannot reinterpret a running attempt or attach print to an old RPC conversation; a genuinely new attempt may select and record a different command. Print recovery launches a fresh CLI in the retained workspace with progress and the unfinished-work reminder, without a taskwatch session path, implicit resume flags or cross-job session reuse.

Print workers and print reviewers share one subprocess supervisor. Each invocation has a new process group, concurrent stdin feeding, and distinct attempt-owned stdout/stderr files streamed directly to disk, not accumulated in memory: `logs/work-a<attempt>.stdout`, `logs/fix-a<attempt>.stdout` and matching `.stderr` files for workers. Raw logs remain evidence; error summaries are bounded excerpts. Missing executables, nonzero exits, signals, failed stdin delivery and timeout use existing [step-failure/recovery handling](#recovery-and-decisions), without backend fallback. A process that prints completion but stays alive is not complete.

`rpc_timeout` is also the absolute print execution limit, measured from process start across stdin feeding and waiting. Every 15 seconds the existing evidence/progress machinery ticks: evidence heartbeats and log bytes cannot extend the deadline, and log output alone is not progress. Changes to the progress/checklist or declared output files update their progress fingerprint, not stdout bytes. Abort, cancellation, stale attempts/stages, timeout and supervisor failures terminate the process group and reap the leader, including same-group descendants left after leader exit. Existing guarded state updates fence late completion; user-systemd still owns reservation/service cleanup. This is not containment of deliberately detached processes or arbitrary filesystem writes.

Host operators own authentication, network access, loaded rules, model/tool selection and noninteractive permissions. Approval text is not a sandbox. In particular, the job-folder progress, checklist, brief, findings, root-cause and declared public-text paths named by `WORKFLOW.md` can be outside the Git worktree. Prepare narrowly scoped write permissions for the actual authorized files before launch; do not grant the entire job folder or publication permission. Reviewers remain independently read-only except for their required reply destination. Print model controls are vendor/host settings, not omp `@task` or `@slow` role mappings.

The runner prepares `WORKFLOW.md` before invoking the worker. A host-owned launch wrapper can read its exact authorized paths before starting a sandboxed CLI; provision permission for creating the named files as well as updating them. Do not infer permission for sibling files or directories from the approval text.

### Standalone print configurations

These complete `jobs-config.json` examples require neither Hermes nor omp. The synthetic `omp-unused.invalid` argv validates but need not exist. The host must supply the selected vendor CLI on `PATH`, Python/shell utilities where used, and `/srv/example/notify-command`: a real sender implementing [Delivery](#delivery), taking `--to`, `--file` and the generated message path, waiting for channel acceptance and returning the specified JSON. `person.invalid` is a synthetic destination to replace, not an operational account. Configure submission checks separately. These are configuration examples, not evidence of an authenticated model run.

Codex worker and independent schema-constrained reviewer:

```json
{
  "version": 1,
  "slots": 2,
  "stall_seconds": 1800,
  "omp_command": ["omp-unused.invalid"],
  "rpc_timeout": 3600,
  "worker_mode": "print",
  "worker_command": ["codex", "exec", "--sandbox", "workspace-write", "--ephemeral", "--color", "never", "-"],
  "review_mode": "print",
  "review_command": ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "--color", "never", "--output-schema", "{schema}", "-o", "{output}", "-"],
  "deliver_command": ["/srv/example/notify-command", "--to", "person.invalid", "--file", "{message}"],
  "max_reviews": 3
}
```

`workspace-write` alone does not authorize every workflow path outside the worktree. The host must prepare the narrowly authorized workflow writes and reviewer reply destination using its supported policy, rather than assuming the sandbox flag grants them. Check `codex exec --help` for stdin `-`, sandbox modes, `--ephemeral`, `--color`, `--output-schema`, `-o` and host-controlled `--add-dir`; this example does not automatically add directories or approval bypasses. See the official [noninteractive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

Claude worker and independent structured-output reviewer:

```json
{
  "version": 1,
  "slots": 2,
  "stall_seconds": 1800,
  "omp_command": ["omp-unused.invalid"],
  "rpc_timeout": 3600,
  "worker_mode": "print",
  "worker_command": ["claude", "-p", "--output-format", "text"],
  "review_mode": "print",
  "review_command": ["sh", "-c", "claude -p --permission-mode plan --allowedTools \"Read,Grep,Glob,Bash(git diff *),Bash(git log *),Bash(git show *)\" --output-format json --json-schema \"$(cat \"$1\")\"", "sh", "{schema}"],
  "deliver_command": ["/srv/example/notify-command", "--to", "person.invalid", "--file", "{message}"],
  "max_reviews": 3
}
```

The worker receives its prompt on stdin. The host must configure its model, tools, writable workflow paths and noninteractive permissions without depending on an interactive approval prompt. No `--bare`, permission bypass or auto-approval flag is added: normal host rule/authentication discovery remains intact. The official [headless guide](https://code.claude.com/docs/en/headless.md) and [CLI reference](https://code.claude.com/docs/en/cli-reference) describe bare mode separately: it skips hooks, plugins and `CLAUDE.md` and requires explicit authentication setup. Verify current bare-mode behavior and permissions against `claude --help` on the target host.

### OpenCode stdin adapter

After copying [`examples/worker/opencode.py`](../examples/worker/opencode.py) to the host as `/srv/example/opencode-worker.py`, replace either complete configuration's worker argv with exactly `["python3", "/srv/example/opencode-worker.py"]`, retaining its explicit print reviewer and sender. The host supplies Python 3 and `opencode` on `PATH`, plus its model, authentication, tools and narrowly scoped permissions.

The adapter validates UTF-8 stdin and passes the entire prompt as one positional argument to the documented `opencode run [message..]` interface, using `--` to end option parsing even for a prompt starting with `--help`. It replaces itself with the CLI, preserving cwd, environment, stdout/stderr, process-group ownership and the CLI's exit/signal semantics. It adds no shell, server attachment, implicit resume or permission grant. It does not assume native prompt stdin support or free inference. See the official [OpenCode CLI reference](https://opencode.ai/docs/cli/) and verify compatibility against `opencode run --help` on the target host.

This argv transport is deliberately limited on Linux: every encoded argument/environment entry including its terminating NUL must fit `min(32 * page_size, 128 KiB)`, and all encoded argv/environment strings, terminating NULs and pointer slots plus a 2048-byte reserve must fit `min(ARG_MAX, 128 KiB)`. This conservative check can reject prompts the kernel might accept; the environment reduces available prompt space. Embedded NULs, invalid UTF-8 and size violations fail before execution. A missing CLI or kernel `E2BIG` produces an explicit diagnostic, never truncation, another transport or retry. Synthetic tests establish protocol fidelity only, not vendor compatibility or authentication.

### Model selection

Each RPC process that taskwatch launches (work, fix, and a review in the default `rpc` mode) chooses its model from its own command, and taskwatch binds the job conversation to that model. After the protocol handshake it reads the launched model from `get_state` (`model.provider`, `model.id`), then opens the conversation with `open_session` carrying `sessionDir` and that `provider` and `modelId`, and sends the prompt only after the open succeeded and `get_state` shows the same model. The saved model of a resumed conversation therefore never answers a prompt; the conversation file, its earlier messages and its directory stay as they were. A retry after a usage-limit failure follows the same rule as any other new process; [Rate limits](#rate-limits) say when it is launched.

taskwatch does not read omp's configuration or route between providers. A command that selects a role gets omp's current role order at every launch:

```json
"omp_command": ["omp", "--model", "@task"],
"review_command": ["omp", "--model", "@slow"]
```

`review_command` may instead be the [reviewer launcher](review-launch.md) whose agent definition selects `@slow`. An explicit model (`--model <provider>/<id>`) or a launcher-owned model option keeps its meaning, and a bare `omp` keeps omp's ordinary startup default. A role lists candidates in order and omp takes the first available one, so a first candidate whose quota is used up is still chosen: edit the role order, and the next launch uses it. Print workers and reviewers get no RPC behavior from this.

The step fails before any prompt, with the transcript untouched, when `get_state` reports no model, when `open_session` is rejected (for example `Model not found`), cancelled or times out, or when the model read back differs from the requested one. It never falls back to the saved model or to a new empty conversation. The omp command must support the public RPC `open_session` model override (`provider` and `modelId` together); it is a documented runtime prerequisite, not a transport version, so protocol v2 alone does not prove it. An older omp has to be upgraded by its operator. The upstream references are [RPC](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md) (Model, `get_state`, `open_session`), [models](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md) (role aliases) and the [CLI reference](https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md) (model selection).

### Reviewer other than omp

Set `"review_mode": "print"` to review with a tool that is not omp (Codex, Claude Code, Gemini CLI, or any program). It needs `review_command` and adds no argument to it. The default is `"rpc"`; a host that sets nothing sees no change. Work and fix independently use [Worker execution](#worker-execution); a standalone setup needs both modes set to print.

In print mode the command runs once per review, in the job's folder, with the review message on its standard input. Besides `{guidelines}` and `{review}`, an argument that is exactly `{schema}` becomes the path of the verdict JSON Schema file (a copy of [`docs/launch/review-verdict.schema.json`](launch/review-verdict.schema.json) in the job folder) and exactly `{output}` the path of a file where the tool may write its last message. The reply must be one JSON object that follows the schema; [`docs/launch/taskwatch-print.md`](launch/taskwatch-print.md) is the text the reviewer reads. The schema has the keys the verdict line and the finding lines have in an RPC review, so the same rules decide what is accepted.

taskwatch looks for the verdict in the `{output}` file first, then in the standard output. In each it accepts the object itself (plain, or in a code fence), the object inside a wrapper (`structured_output` or `result` of `claude -p --output-format json`, `response` of `gemini -p --output-format json`), or the last line of a JSON-lines stream that holds it. Whole reply documents and individual JSONL candidates have a 4 MiB parsing bound; oversized single candidates explicitly fail rather than being truncated into a verdict. Raw files are retained, while malformed-reply excerpts in review records are limited to 20,000 characters. It validates the object against the schema and fails the round with the schema path of the first errors (`$.findings[0].severity: 'P9' is not one of ...`) when it does not match. There is no session, so there is no same-session repair: a malformed reply uses its review attempt and the review runs again. A nonzero reviewer exit fails the step unless its stderr holds an explicit terminal HTTP 429 diagnostic, which is a [rate limit](#rate-limits). Deadline, stdin-delivery and supervisor failures remain execution failures even if stderr mentions 429. Both raw streams remain in `logs/review-a<attempt>.stdout` and `.stderr`; both callers use the lifecycle in [Worker execution](#worker-execution).

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

### Review limit

Optionally add `"max_reviews"`, an integer from 1 to 3, to set how many reviews one cycle may run. The first review counts, so `1` reviews once and stops for the owner when it fails, and `3`, the default, allows the first review and two more. A submission may carry its own `max_reviews` with the same range. The per-job value wins over the host value, and the host value wins over the built-in default. The result is frozen into the job when it is submitted, so a later change of the host setting does not change a job that exists. A boolean, `null`, a string, a fractional number or a value outside 1 to 3 is rejected, and a submission that carries one is refused before anything is stored. The `init` draft writes the default.

```json
"max_reviews": 2
```

A review is counted when it is reserved, before it starts, and never after it returns. [Recovery and decisions](#recovery-and-decisions) lists what the count includes and how a resume, an older record and a rate limit treat it.

### Job environment

Optionally add `"env"`, an object of string keys and string values, to set the environment of every process the runner starts for a job: RPC and print workers/reviewers, check command, runner git/forge commands and enabled install hook. A systemd job starts with a minimal `PATH`, so tools such as `gitleaks` in `~/.local/bin` or omp plugins in `~/.bun/bin` can need entries here. Values are used as written, except in `PATH`, where `~` and `$HOME` expand and `$PATH` stands for the inherited value. No shell runs implicitly. Anything other than an object of strings is a config error; omitting `env` or leaving it empty keeps the inherited environment. The `init` draft omits this key.

```json
"env": {"PATH": "~/.local/bin:~/.bun/bin:$PATH"}
```

### Owner defaults

Optional `<root>/taskwatch/defaults.json` holds standing owner answers for design jobs (`{"version": 1, "defaults": [{"id", "value", "scope": "global" | "<target path>", "source": "<owner words>"}]}`), validated against [`docs/jobs/defaults.schema.json`](jobs/defaults.schema.json). Every design job's work and review messages name the file and its short SHA-256 hash (`Owner defaults: <root>/taskwatch/defaults.json (sha256 <12 hex> | absent)`); see [`docs/jobs/design.md`](jobs/design.md#choicesjson) for the scope precedence and `choices.json` check rules.

### Code job work folders

A `code` job that builds a result works in `state/jobs/<id>/worktree`, on its own branch.

- Base. Before creating a worktree without an explicit base, the runner runs noninteractive `git fetch origin <default branch>` with a 5 second cap; the branch is the one `origin/HEAD` names, or `main`. It starts from `origin/<default branch>`; a failed fetch or missing origin records `base_note: "base may be stale: <reason>"` and uses the local ref. An explicit `base` commit or branch is used without that creation fetch. `base: "@<receipt key>"` retains the original predecessor relationship: a nonlanding predecessor supplies its branch, while a landed predecessor supplies verified immutable squash commit S, not a branch that cleanup can delete. Workspace creation pins the effective base commit. Landing always requires a fresh successful fetch, regardless of the creation fallback; see [Automatic landing](#automatic-landing).
- Branch. `<branch_prefix><first 8 characters of the job id>`. Optionally add `"branch_prefix"` to `jobs-config.json` (default `"taskwatch/"`): letters, digits, `_` and `-`, with parts joined by `/`. A resumed job reuses its worktree and branch.
- Cleanup. When a job reaches `done` or `cancelled`, and not at `deliver_pending`, the scan runs `git worktree remove` without `--force`. A folder with uncommitted or untracked files, or that git refuses to remove, stays and `workspace_state` says why (`kept: <reason>`); the scan retries it. The job folder, records, reviews and logs are never deleted.
- Branch deletion. After a fetch, the branch is deleted only when it is integrated: its tip is in the history of `origin/<default branch>`, or merging it onto that branch changes nothing (a squash merge already holds its content). Otherwise `branch_state` stays `kept` and the scan checks again later. `integrated` means integrated but git refused the deletion. The integration check needs git 2.38 or later (`git merge-tree --write-tree`); on an older git only a branch in the history is deleted.
- Record fields: `base`, `base_note`, `branch`, `workspace_state` (`active`, `removed`, `kept: <reason>`) and `branch_state` (`kept`, `integrated`, `deleted`), shown by `taskwatch show`. Records from before these fields exist keep their detached worktree and are not cleaned up.

### Job records

A job keeps its checklist, progress and brief files in its job folder (`state/jobs/<id>/`), next to the logs and reviews, not in the workspace. The worker prompt and `WORKFLOW.md` give their absolute paths. The records are therefore never part of a result, and the clean-tree check of a `code` job needs no exception. `WORKFLOW.md` also carries the brief contract and the rules of the job's type from `docs/jobs/`. A job submitted earlier whose paths name `.taskwatch/...` still loads; those paths now resolve inside the job folder as well.

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

`target` must exist. `review` picks the review guidelines the reviewer reads: `code`, `design`, or `source` (a document that cites sources, such as research notes, summaries, or comparisons). Only `code` also runs the work in a Git worktree, because a code review reads the Git diff. A job with `review` `code` or `design` and a work contract needs a spec that follows [docs/spec-template.md](spec-template.md): each of its thirteen `## ` headings is present and answered, or says `n/a: <reason>`. Submit refuses a missing or empty heading by name, and a bare `n/a`. A `source` job and a review-only job skip this check. For these two job types the reviewer's verdict line also carries a `design_check` of eight answers (`root_cause`, `alternatives`, `one_home`, `overlaps`, `needed_now`, `repeated_judgment`, `portability`, `choices`), each `ok`, `finding` or `na` with its evidence; the runner checks only its shape. The review message names `approval_source` and `approval_scope`, so the reviewer can tell an owner decision from a writer's choice and compare the writes of the change with the scope. Output paths are relative to the job workspace, checklist and progress paths to the job folder; none can traverse outside its base. `check_command` runs as argv without an implicit shell; name a shell explicitly only when the approved check needs one. The work must create that check script if it is not already in its workspace. The checklist follows the existing nonempty `items` list of boolean `passes`.

For jobs with a work contract, generated workflow and work, fix and resume messages grant writes outside the workspace only to the configured `progress` and `checklist` paths, `brief.md`, `findings.json`, `root-cause.md`, and the declared public-text files below.

The exact three public-text grants are `pr/<id>/title.txt`, `pr/<id>/body.md` and `pr/<id>/commit.txt` for the declared `public_text` item only.

Instructions enumerate the actual job-specific file paths, including nested configured record paths, not a writable folder or wildcard.

Only parent directories needed for these named files may be created; directory deletion and writes to other job-folder files are not authorized.

All these worker-owned files stay outside the workspace and must never be copied into the committed result.

Findings are written when repairing review findings; root-cause evidence is needed for repeated rules.

Neither is mandatory for an untouched initial result.

The exception does not include the original prompt, copied inputs, `WORKFLOW.md`, reviews, snapshots, logs, sessions or global state. Review-only jobs receive no worker-owned file exception. Preparing declared public text grants no authority to publish it. Any action outside the recorded scope and named file exceptions still requires the existing wait-before-action approval procedure.

`inputs`, `predecessors`, `expected_wait_seconds`, `approval_required`, `max_reviews`, and `land` are optional. Every other example field is required. Each entry of `predecessors` is an existing job's id or `receipt_key` (id first, else `receipt_key`); the stored record keeps the resolved job ids. Derive the receipt key from the approval message and its job number. A repeated key returns the original job even if other arguments differ. The command records the caller's approval assertion; it cannot authenticate the conversation.

`max_reviews` is the review limit of this job. When it is absent the job takes the host setting; see [Review limit](#review-limit).

A job with `predecessors` gets one block per predecessor in the work and review messages: its id, job type label (`research` for `review: source`, `design`, `implement` for `review: code`, `review` for a `review_target`), stage, result location (the output files in its workspace, or for a code job the branch `<branch_prefix><first 8 of the id>` in its target), result fingerprint and the path of its `brief.md`. The block is read only: nothing is copied, so input hashes and the review basis do not change, and the spec still says how to use the result.

`approval_required` defaults to `false`. If `true`, the first scan moves the job to `approval_wait` before its first work run. With `predecessors`, the job stays `queued` (wait reason `predecessor:<id>`) until every predecessor is `done`, so the owner has the predecessor's result before the question; a cancelled or `user_wait` predecessor leaves it queued. A job without `approval_required` still starts once its predecessors reach `deliver_pending`. `approve` clears the flag and allows work to start. A later `wait` records the current step for approval.

`approval_scope` is a recorded boundary, not operating-system isolation. The runner's shell tools have the user's permissions; instructions and explicit approval records cannot enforce those permissions.

### Public text

Code jobs that build a result accept an optional `public_text` list with at most one publication unit covering the complete job result.

The item shape remains `{"id": "pr1", "base": "<ref>", "head": "HEAD"}`; `head` defaults to `HEAD`, and `id` is lowercase letters, digits and hyphens.

A repository that requires the squash commit to be the pull request title with a blank body lets the item add `"blank_squash_body": true`. The key is optional and, when present, must be `true`; it is stored only when declared, so existing job records stay valid and a unit without it keeps the mandatory squash body.

The item's external `base` resolves and is pinned at submission; checks require that commit to equal the job's effective workspace base, and its `head` must resolve to the complete result `HEAD`.

Partial ranges, alternate heads and several publication items are rejected; equivalent refs are accepted by resolved commit identity.

Independent pull requests require independent jobs; stacked jobs use the job-level `base` and `predecessors` described in [Chains and gates](#chains-and-gates), not multiple publication units in one job.

The worker writes `pr/<id>/title.txt` for the pull request title, `pr/<id>/body.md` for its evidence and known limits, and `pr/<id>/commit.txt` for the squash commit body, which is mandatory unless the item declares `blank_squash_body`.

`commit.txt` is body-only, not a complete commit message and not an optional description; the proposed squash subject comes from `title.txt`. When the item declares `blank_squash_body`, the file must still exist but must be blank (empty or whitespace only), and the squash message is the title alone.

All three files live in the job folder, never in the work folder.

The check rejects missing or blank required texts (a blank `commit.txt` only when the item does not declare `blank_squash_body`, and a nonblank one only when it does), an empty commit range, or invalid text and commit message shapes; [Checks](checks.md#public-text-evidence) owns validation, generated fingerprints, read-only snapshots, reviewed copies and historical status.

Preparing or copying reviewed text does not publish, push, merge or authorize landing.

The check command must exit `0`. Required outputs and a nonempty passing checklist must also exist after the command finishes. Minimal checklist:

```json
{"items": [{"passes": true}]}
```

No other checklist item fields are required. A `false` item, missing file, invalid JSON, or empty list blocks progress. The independent reviewer returns exactly one verdict JSON line followed by findings, for example:

```text
{"verdict": "PASS", "blocking": false, "reviewer": "independent-review", "checked": "the diff against the spec", "not_checked": "runtime behavior"}
No blocking findings.
```

A FAIL, malformed verdict, missing input, or blocking finding prevents `deliver_pending`. See [review records](checks.md#review-records) for the shared contract.

#### Job records

The paths here are relative to `<root>/taskwatch/state/`. Use `taskwatch --root <root> show [<id>]` for an authoritative read or `taskwatch --root <root> scan` for normal scheduling and recovery. Read [What needs the owner](#what-needs-the-owner) for the list identities and wake conditions; do not edit storage files to acknowledge an item.

Submission copies and hashes intent, spec, and role-specific inputs. The original prompt is kept intact. `jobs/<id>/state.json` is the sole current full snapshot, in a version-2 envelope with a positive revision and a SHA-256 checksum covering both the revision and all job fields. `jobs-store.json` records the format, stream generation and job-id-to-receipt membership. Receipt keys are immutable and globally unique. An indexed missing or corrupt snapshot is an error, not an absent job; unindexed folders left by input copying are not jobs. Corruption blocks scheduling rather than allowing a job to start or pass.

Writers share the existing reentrant state lock. Each save validates its affected snapshot, membership and unseen committed event tail, rather than rereading obsolete full snapshots or unrelated jobs. A cold load validates the indexed current snapshots. `job-changes.jsonl` holds checksummed version-2 events with generation, sequence, job id, revision and only the needs projection: stage, wait reason, wait/approve/resume entry count, predecessors and last event time. Every successful save emits an event, including an unchanged projection; needs identities, not save count, determine a wake. Full event/review histories remain in current snapshots and may grow. The small log grows linearly with saves; there is no automatic rotation or bound on total disk use.

A save writes and fsyncs a restrictive same-directory temporary snapshot, then atomically publishes and syncs `jobs-pending.json`, a small prepared transaction containing the temporary basename, revision/digest, exact event bytes and any new receipt membership, never another full snapshot. It replaces and syncs the snapshot, publishes new membership if needed, appends and fsyncs the event, then removes the pending marker and syncs its directory before returning success. These files are **not** independently updated atomically. Failure before the pending marker leaves the old state authoritative and may leave an unreferenced temporary file. Failure after publication is uncertain to the caller: the next locked authoritative read or mutation rolls the transaction forward, validating its revision, digest and sequence. An already matching event is synced again, not duplicated; receipt retries retain the original identity. Torn final events are preserved beside the log before repair. No external operation runs in storage recovery.

On first authoritative `show`, normal `scan` or mutation without a format marker, the writer validates every committed legacy snapshot once, including checksums, versions, typed fields, ids and receipt invariants. It preserves a torn suffix, stages latest states and one initial projection per job, syncs them, and publishes the index last. Interrupted pre-marker migration accepts only artifacts matching the exact validated legacy latest states; conflicts or orphaned v2 files without a legacy source fail closed. Empty roots initialize the same format.

After cutover, `jobs.jsonl` and `jobs.cursors.json` remain untouched historical backups: no dual writes, automatic deletion or movement. Stop old workers and watchers before replacing binaries; an old binary is not a supported v2 writer. Back up the index, current states, change log and pending/temporary files together with writers stopped or under the same lock. Rollback means restoring a consistent pre-migration backup with every process stopped, not running an old writer against v2 files.

If validation refuses a stable file or migration conflict, inspect the command error and scan service journal; do not remove the marker or adopt orphan files. Restore a consistent backup with all processes stopped rather than bypassing checksums or receipt checks. Keep copied inputs and execution evidence when backing up the job folders, as well as the other state ledgers.

`needs` remains unlocked and read-only: it follows committed version-2 projection bytes, whose format is unchanged, and loads validated, revision-matched current snapshots for rendered jobs, required gate predecessors and new finished candidates classified by the [wake policy](#what-needs-the-owner). Classified snapshots are cached by generation, job id and revision. It rechecks the event boundary, generation and every rendered or classified snapshot before returning. A selected newer snapshot without its committed event defers the result only when a matching pending marker exists, preserving the incoming cursor; a non-wait call explains how to recover with `show` or normal `scan`, while a wait keeps polling. A watcher never repairs a crashed writer. Until the next normal locked read/scan recovers it, an unpublished transition need not be visible. A stable snapshot/event mismatch without a matching pending marker is corruption.

Cursors remain opaque nonnegative integers, now identifying the v2 generation and committed byte offset. Retained v2 cursors resume precisely. Legacy byte-only and stopgap-generation cursors, future offsets and offsets splitting a committed line conservatively replay current actionable and finished jobs with an explanation and a new cursor when a result is returned; waits still apply the [wake policy](#what-needs-the-owner). This is a one-time cutover replay, not a new wake policy. First-use `needs` never migrates: legacy or staged files without a marker produce error code 1 and an instruction to run `show` or normal `scan` once. A genuinely empty root returns no items and cursor 0 without writing; a waiting watcher observes the first published generation normally.

Each job has `state/jobs/<id>/`, a separate workspace, copied inputs, a `WORKFLOW.md`, and execution evidence. Code jobs use a git worktree on their own branch; see [Code job work folders](#code-job-work-folders). Results are not merged or installed unless [automatic landing](#automatic-landing) was explicitly enabled at submission.

#### Review-only jobs

Replace `outputs`, `check_command`, `checklist`, and `progress` with one `review_target` to review something that already exists. There is no work stage and no check; the job starts at the review step, in the target directory itself.

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

`review_target` is exactly one of `{"files": ["relative/path"]}` (files under `target`) or `{"commit": "<revision>"}` (resolved to a full SHA when the job is submitted). `inputs`, `predecessors`, `expected_wait_seconds`, `approval_required`, and `max_reviews` work as for other jobs.

The verdict is the result. A parsed `PASS` or `FAIL` is recorded and delivered; a `FAIL` does not start a fix, because there is nothing to fix. An answer with no acceptable verdict line, or a target that changed during the review, uses its review attempt and reviews again, up to `max_reviews` attempts in all; after that the job stops for user judgment (`review_exhausted`). The review record and job state are in `show <id>`.

### Automatic landing

The optional submission boolean `land` defaults to `false`, including for historical jobs. Nonlanding jobs retain local-result delivery and need neither `gh` nor an install hook. `land: true` requires a result-building `review: code` job, exactly one complete-result `public_text` declaration and nonempty `approval_source` and `approval_scope`. It is the caller's assertion that the recorded authority covers publication, merge and any enabled install hook, not authentication of a conversation or keyword matching of natural-language scope. Neither worker output nor an accepted PASS grants that authority. Existing `approval_required` gates still apply; editing live files cannot make a historical job landable.

The checked `text_contract: 2` marker is generated after work/check/review, not accepted as a caller `public_text` key. Landing shares delivery's acceptance checks: accepted PASS, clean code fingerprint, declared-output digest, basis and guideline hashes, unchanged checked texts, snapshot digest and accepted check identity/log hash. Only reviewed snapshot publication bytes are used.

#### Host install command

The optional host `jobs-config.json` key `install_command` defaults to `null` (disabled). When enabled it is a nonempty argv list of nonempty strings, not a shell command or per-job executable override. Include `{sha}` for the full observed squash commit S; optional `{target}` expands to the persisted absolute repository target, which is also the working directory. For example, a host-trusted wrapper can be configured as:

```json
"install_command": ["approved-installer", "--sha", "{sha}", "--target", "{target}"]
```

A recognizable pinned source is an argv element that is a `git+https` or `git+ssh` URL ending `@{sha}`, or an HTTPS GitHub archive URL containing `{sha}`. Its host, owner and repository must match the transaction origin and its expanded pin must be S, otherwise execution is rejected. Any other argv is a host-trusted wrapper: its contract is to verify the target's origin and exact S before installation, never install a mutable checkout. Selection follows argv shape, not a mode flag. taskwatch does not prove wrapper correctness or infer it from the executable's name.

The hook identity, expanded argv and hash are frozen for the transaction, including when the hook is disabled. A changed or disabled host configuration parks as `land_hook_changed`; resume cannot adopt a replacement. Restore the frozen configuration or cancel. Execution uses the job environment, noninteractive stdin, captured exit/log and an independent 300 second timeout, with no implicit shell. This is separate from L5's CI deadline.

#### Lifecycle L1 through L8

The deterministic `land` step runs in the existing transient service, not a model, and emits evidence heartbeats every 15 seconds. R is the accepted result commit, T its tree, B the accepted base, M freshly fetched `origin/<default branch>`, H the publication head and S the observed squash commit.

1. **L1, accepted identity.** Require the shared freshness evidence, contract-2 snapshot and one entire-result unit whose head/tree is R/T. Before publication stale evidence follows existing check/review recovery; after publication it parks as `land_review_stale`, preserving identity.
2. **L2, base.** Require a successful fetch and M=B, or M exactly equal to a declared same-repository predecessor's successfully landed S with tree(B)=tree(S), verified final tree and successful installation when configured. Otherwise park as `land_base_moved`; there is no stale-fetch fallback, cherry-pick, rebase or automatic conflict resolution.
3. **L3, publication head.** Construct H with `git commit-tree T -p M`, using the reviewed title, one blank line and reviewed `commit.txt`; a unit that declares `blank_squash_body` uses the title alone. Verify its parent/tree and persist H plus commit metadata before pushing the deterministic `<job.branch>-land` branch. Retries reuse that object, not a new timestamp. An absent remote ref permits a normal same-H push, equal H resumes, and foreign H parks as `land_head_changed`; no blind force push.
4. **L4, pull request.** Search all PR states by exact repository/head/base before creating. Reuse only one matching H, title, body and deterministic evidence footer; creation uses `--body-file`. Duplicate, edited or CLOSED-unmerged PRs park as `land_pr_mismatch`; ambiguous lookup parks as `land_interrupted` without creation. Persist the PR number/URL, never generate replacement prose or a replacement PR.
5. **L5, CI.** Persist `started_at` and absolute `deadline=started_at+rpc_timeout` at the first observation after PR creation or reconciliation. Query registration immediately and again at 15 seconds if absent; missing registration after grace parks. Poll registered pending work every 20 seconds within that deadline. Require all registered checks/status contexts and all PR CI workflow runs for H, including optional workflows, to finish successfully, with at least one successful CI workflow run. No required checks is acceptable only with nonempty successful CI; missing, stale, empty, skipped-only, cancelled or failed evidence is not green.
6. **L6, merge.** Immediately fetch/recheck M, exact PR H/base/text, mergeability, supported capabilities and green CI identity. Validate the final subject including deterministic ` (#n)` suffix under existing message rules, without rewriting it. Request head-pinned direct squash with reviewed snapshot commit body (an empty body file when the unit declares `blank_squash_body`); only observed MERGED with full S is success. Refusal parks as `land_merge_failed` or a specific mismatch. Exit zero, a queue, auto-merge or unknown outcome is not success; no `--admin` or protection changes.
7. **L7, final tree.** Persist discovered S before fetching and verifying tree(S)=T, current `origin/<default branch>` contains S and its tree is T; record that observed main SHA too. A mismatch parks as `land_tree_mismatch` with the merge visibly retained, no installation, rollback or replacement PR.
8. **L8, install and deliver.** After L7, run only the approved frozen host hook or proceed if absent. Successful installation or no hook reaches `deliver_pending`, then the existing card/send-once/unknown-delivery policy. Normal cleanup follows delivery and never force-deletes dirty or unintegrated worktrees or job records.

The CI expected set is protection/ruleset required checks plus everything registered for H after registration grace, using the latest attempt for each run. A workflow suppressed by path or other filters that never registers cannot be discovered through this evidence; landing does not claim to prove such a workflow ran.

One rerun round freezes all then-failed H Actions run IDs. Each ID's intent is saved before `gh run rerun --failed` and spends that ID's budget even if acknowledgement is lost. Recovery may issue unissued members of that frozen round, never add IDs or repeat issued intents. A frozen run failing again, a new failure after the round or deadline expiry parks as `land_ci_failed`. Automatic restart cannot reset the deadline. A distinct explicit owner resume from that stop may open a fresh observation window/grace only after revalidating H/base/text, retaining the same transaction/PR and consumed rerun budgets. The CI window spans L5 only and cannot block an explicitly resumed install retry after merge.

Head pinning does not atomically pin the merge base: an external writer can move the default branch after L6. L7 detects a wrong final tree and prevents installation but cannot undo a completed merge. Repository serialization protects only taskwatch and cooperating processes, not arbitrary external writers.

#### Reconciliation, stops and restart

`land_state`, default empty and frozen with the job snapshot, retains phase/transaction identity, R/T/B/M/H, repository/default branch, review/check/text/snapshot digests, deterministic branch and operation intent, PR identity, CI IDs/deadline/rerun budgets, S/observed main, frozen hook/argv hash/outcome/log and last error. Writes use existing attempt/stage CAS, `Jobs.save`, state lock and Store prepare/recover; the v2 current snapshot remains authoritative, with compact projection events rather than a second landing ledger. Storage recovery never performs external actions.

Every external write has a durable intent before invocation and an observed outcome afterward. Recovery queries the exact remote H and saved/discovered PR. OPEN requires all guards again; MERGED resumes L7, never a second merge; CLOSED-unmerged stops. After a saved create intent, even a successful all-state lookup with zero matches remains unknown and parks as `land_interrupted`. A distinct explicit owner resume repeats the lookup and, only if still zero, authorizes one create by atomically consuming that resume entry's token with the next create intent. Automatic restart or replay of that entry grants no new create.

Install outcomes are `not_started`, `started`, `succeeded`, `failed` and `unknown`. Persist `started` before invocation; nonzero exit is `failed`, interruption or timeout is `unknown`, and confirmed exit zero is `succeeded`. Failed/unknown outcomes park as `land_install_failed` with the concrete outcome and never automatically replay. Each distinct explicit owner resume entry authorizes one retry of the frozen argv after rechecking S/T/current main; its token is persisted and consumed atomically with `started` under attempt/stage CAS. Restart or replay of the same entry cannot grant another retry; a later distinct owner resume can. Success skips the hook even if delivery fails later.

Landing stops use `user_wait` with `resume_stage: land` and retain their facts for `show`/`needs`; use existing `resume` or `cancel`, not a generic retry option. Resume does not approve changed external input or publication content: those require existing check/review. After merge, freshness failure retains S and parks, never requeues a new publication, merge or install. Unknown result delivery still requires explicit acceptance of duplicate-message risk before resending the card.

L1 through installation is serialized by the canonical real git common directory, not target spelling: scheduler reservations and an inherited nonblocking lock there cover aliases, linked worktrees, orphan subprocesses and other taskwatch roots. Reservations survive `user_wait` while an old unit may live and are released only after confirmed death, never an ambiguous systemd result. Stale/cancelled attempts abort child process groups. Long git/forge/CI/hook operations run outside the global state lock; independent work/check/review worktrees remain parallel.

The original `@receipt` relationship is durable. At successor workspace creation a landed predecessor must satisfy existing downstream readiness plus recorded merge, verified S/tree and installation success when configured; the immutable effective base resolves to S even after its branch is cleaned. Nonlanded predecessors keep branch-based behavior. Multiple predecessors order jobs, not automatically combine branches.

### Timer entry point

Install the scan timer with the units that `init` stages; see [Installation](#installation). The timer is the fallback and keeps its five-minute schedule. A scan also runs two seconds after a step ends and after `submit`, `approve` and `resume`, through `systemd-run --user --on-active=2s`; a failed wake only prints a warning. A failed step does not wake, so the timer spaces its retries.

`taskwatch --root <root> init --unit-dir <empty-unit-staging-directory>` writes `agent-taskwatch-scan.service` and `.timer` without installing, reloading, or enabling them. Existing files are refused. The service calls the standalone `scan` with the current interpreter and explicit root. Inspect the staged files and replace the existing service only as part of an approved installation. Run `systemctl --user daemon-reload` after installing it. Confirm the timer is enabled and active, and confirm linger with `loginctl show-user <user> -p Linger` if jobs must survive logout. Run these installation steps only on the intended host with approval.

`scan` is a short scheduler, not a resident dispatcher. It reserves the job attempt durably before invoking `systemd-run --user`. The deterministic `taskwatch-<id>-a<attempt>.service` name allows a later scan to find a start whose acknowledgement was lost. `taskwatch run <id> --attempt <n>` is an internal step command; do not run it alongside the service.

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

The `init` draft sets `deliver_command` to `["false", "{message}"]`, which intentionally cannot send. An attempt with this draft stops for user judgment, so replace it before enabling the timer.

`jobs-config.json` requires `deliver_command`: a list of nonempty strings run as argv without a shell. Exactly one argument must be the whole string `{message}`; it is replaced with the absolute path of the generated message file. The destination is a literal argument written in this command, not the job's `target` (a workspace path). Example, with a synthetic destination:

```json
"deliver_command": ["hermes", "send", "--to", "discord:#synthetic-channel", "--file", "{message}", "--json"]
```

Choose a configured Hermes target with `hermes send --list`.

Hermes is optional. Without it, provide a sender executable for your channel and configure its argv instead, for example `["notify-command", "--to", "person.invalid", "--file", "{message}"]`. The sender must wait for the channel server's response and implement this contract: stdout must contain exactly one JSON object, with no diagnostic text around it. Write diagnostics to stderr. Both streams and the exit code are recorded. For example, exit 0 with `{"success": true, "message_id": "synthetic-message"}` confirms sending. Every other outcome is unknown, including a nonzero exit with a readable JSON `error`: a timeout or partial send may already have delivered content.

For a job at `deliver_pending`, `scan` runs this step under the state lock after the old unit has been released. It writes `delivery.txt` and `delivery-review.txt` in the job folder, records the attempt as `started` with the message file and the SHA-256 of `delivery.txt`, then runs the command with a fixed 60 second timeout. `delivery.txt` is the card and nothing else: the conclusion from the worker's `## Card`, a blank line, then only the lists that have content under their [`language`](#language) headings: the worker's bullets (`Changed`), one bullet per P2 or P3 finding of the last review that has a `summary`, worst first (`Small problems left`), and the items of the reviewer's `not_checked`, split on `; ` (`Not checked`). A list shows at most 6 bullets of at most 160 characters; a seventh shows as `+N`. The message has no paths and no pick block, so it stays under Telegram's 4096 characters. The full decision brief is composed on read by [`brief`](commands.md#brief), which lists its contents and exit codes.

Before sending, the result fingerprint, declared output content digest including Git-ignored files, review basis and checked text evidence must match the accepted PASS, using the same acceptance policy as landing. Changed, missing, or unreadable evidence normally returns to `check`, spending the existing recovery budget; exhaustion stops at `user_wait/result_unstable`. After publication, stale evidence parks as `land_review_stale`; after a recorded merge it retains S and never requeues into another publication, merge or install.

After scheduling work and result deliveries, each scan sends one short stop notice for every new `user_wait` or `approval_wait` entry through the same `deliver_command`, timeout, and success contract. `notice-<index>.txt` names the job, first prompt line, stage, wait reason, error, and resume/approve/cancel commands. Attempts are reserved in `notices` under the state lock before sending. The sender runs without that lock; only its notice result is merged into the latest snapshot, so unrelated jobs can finish while it waits. No outcome causes an automatic retry or changes the stage. Interrupted attempts become unknown. `delivery_unknown` receives a separate notice, never a result resend.

For result delivery, success needs exit code 0, a JSON object with a truthy `success`, no `error`, and a present `message_id`. Then the job becomes `done` and is never sent again. Success means the command reported acceptance; it is not a read receipt. A response with only `skipped` is unknown, not done.

- Every non-confirmed outcome (including a readable `error`, timeout, unreadable output, signal, ambiguous response, or an earlier attempt still `started`) moves the job to `user_wait` with reason `delivery_unknown`. It is never resent automatically. Hermes provides neither lookup nor an idempotency key.
- `resume <id>` restores `deliver_pending` for one new send and keeps the attempt history; an interrupted `started` attempt is recorded as unknown. Resume only when a duplicate message is explicitly accepted, because the earlier message may have been delivered. `cancel` prevents further sends.

### Checking a result before it ends

[`check`](commands.md#check) shows what the check step would reject about a result before the job ends; its output, options and exit codes are in the command reference. `WORKFLOW.md` names the command in its End section as the last step before the agent finishes.

`WORKFLOW.md` tells the worker to run only the tests that cover the files it changed while working, and `check <job> --run-command` once just before it ends.

### Chains and gates

A job type is a label derived from `review` and `review_target`; it names the contract in [docs/jobs](jobs/brief.md) that `WORKFLOW.md` carries. There is no job type field in a record or a submission.

| Job type | Fields | Result | Checked by code |
|---|---|---|---|
| research | `review: source` | `result.md`, `claims.json`, `sources/` | source check (when `outputs` has `claims.json`), checklist, brief |
| design | `review: design` | `design.md`, `choices.json` | `choices.json`, `check_command`, checklist, brief |
| implement | `review: code` | committed branch | the repository's tests as `check_command`, clean commit, brief |
| review | `review_target` set | the review record | none (no work stage) |

Publishing is a gate, not a job type. The common chain is research, a gate, design, a gate, implement; a small change can skip the first two, and a design job with `claims.json` in `outputs` includes its own research.

The submit rule for a caller that sends ordered units under one approval:

1. Submit the units in order, each with the receipt key `<approval message>:<n>` and `predecessors` naming the unit before it by its receipt key (or its id). Receipt keys permit submitting a ready chain in one pass; the stored record keeps resolved ids. A successor lists each predecessor's result, so no pasted path is needed. For a stacked code unit prefer `base: "@<predecessor receipt key>"`: it preserves a landed predecessor's verified immutable S even after branch cleanup, while a nonlanded predecessor still supplies its branch. Multiple predecessors order jobs, not combine branches. Units without ordering can be submitted together without `predecessors`; [Automatic landing](#automatic-landing) owns the extra landed-readiness requirements.
2. A unit that needs the owner's decision on an earlier result (a design before its implementation) carries `approval_required: true`. It stays queued until every predecessor is `done`, then waits in `approval_wait` with the brief of the predecessor ready. Show the owner the output of `taskwatch brief <predecessor>`, then pass the owner's reply to `approve --reason`, preceded for picks by one line `PICKS: ok` or `PICKS: <id>=<key|text> ...`. The reply is appended to the approval source, so the worker and the reviewer see it as the owner's decision.
3. A unit whose `spec`, `intent` or `inputs` must be a predecessor's output cannot be submitted early: those files are copied and hashed at submit. Submit it after the output exists.
4. A cancelled or stopped predecessor leaves its successors queued; `needs` lists them as blocked. Cancel or resubmit them.
5. Arm one `needs --wait --from <cursor>` for the room instead of one `watch` per job.

### What needs the owner

`taskwatch --root <root> needs` lists what waits for the owner and prints an opaque integer `cursor`. Pass it unchanged to `--from`; do not calculate it from file size. It is read only: no lock, nothing written, derived on every call. See [Job records](#job-records) for generation and cutover replay semantics, and [commands](commands.md#needs) for usage.

Sections: `GATE` (a job in `approval_wait`: with `predecessors` it names them and their briefs and counts the decisions in their `brief.md`, or the picks of a design predecessor with the reply template `ok | <id>=<key> | text`; otherwise it shows the reason), `NEEDS YOU` (a job in `user_wait`: reason, review round, open findings, rules that repeated in earlier reviews), `BLOCKED` (a queued job behind a cancelled or `user_wait` predecessor) and, only with `--from`, `FINISHED SINCE THE CURSOR` (a job that is `done` now and was not at the cursor, with its verdict, decision count and open findings). At most 20 jobs are shown. A non-wait call keeps listing finished jobs regardless of the wake policy.

`needs --wait --from <cursor>` waits for a new `GATE`, `NEEDS YOU` or `BLOCKED` item, or a newly finished job that needs attention; [commands](commands.md#needs) lists its options. An item is `(job, stage, reason, entry)`, where `entry` counts the job's `wait`, `approve` and `resume` events, so a job that is answered and then stops again for the same reason wakes the watcher, and a re-save of the same stop does not.

A newly finished job stays quiet only when its latest review is a `PASS` accepted by `job_findings.accepted` with at least one of `ok` or `final` equal to `true`, that review has no findings, its brief passes `brief.problems` and has zero decisions, and, for a design job, `choices.json` is valid and has no picks. An absent or unknown verdict, an unaccepted or failed review, any remaining findings, owner decisions or design picks, and missing, unreadable or invalid review, brief or choices content wake conservatively. Job-store corruption is still an error, not a content problem converted into a wake.

For a result-building code job, brief file and line pointers are checked against the accepted result commit, not the current target checkout or a worktree that normal delivery cleanup may remove. Job-folder evidence remains available as a fallback for paths absent from that commit. Missing result commits, invalid pointers and malformed briefs still wake conservatively.

`--include-done` makes every newly finished job a wake trigger and is valid only with `--wait`; `--wait` always requires `--from`. On a wake, the capped list prioritizes new triggering items, then current waiting items, then clean finished jobs, so a clean completion cannot displace the reason for waking. Clean finished jobs may appear alongside a trigger without triggering a wake themselves. If more than 20 items need attention, use `show` to inspect the omitted jobs before re-arming: the returned cursor covers the whole committed boundary, not only the displayed items. Wake filtering does not change [result delivery](#delivery): the configured delivery command still sends every card unchanged, including for quiet completions.

Flags, exit codes and how to re-arm after each exit are in [commands](commands.md#needs). Keep only one watcher per room instead of one `watch` per job:

```python
terminal(
    background=True,
    notify_on_complete=True,
    command="taskwatch --root <root> needs --wait --from <cursor>",
)
```

The list is a hint; re-read the state with `show` before acting ([commands](commands.md#show) describes the record and its derived `activity` key). See [Job records](#job-records) for storage, recovery and cursor cutover. Landing stops expose the concrete reason and retained PR, merge and install facts; inspect `land_state` with `show` before resuming or cancelling. An example skill for this loop is in [`examples/hermes/skills/taskwatch/SKILL.md`](../examples/hermes/skills/taskwatch/SKILL.md).

### Recovery and decisions

The job retains its parallel slot through work, checks, review, fixes and landing. A job that works in a shared folder (a non-code job, or a review-only job) locks its target across these stages; a result-building code job has an independent worktree, but landing additionally reserves its canonical repository through install (see [Automatic landing](#automatic-landing)). A blocked predecessor leaves the successor queued. An unrelated target can still start.

Dead or reboot-interrupted runs resume only after the old service is known to have stopped. An unknown service outcome requires user judgment. Unchanged progress and output content first produces a stall suspicion; an unchanged scan at least 300 seconds later stops the service and recovers. Log growth alone is not progress. Expected waits delay stall recovery.

When a run is found dead and the filesystem of the job folder or of `TMPDIR` (the configured `env` value, else the system temporary folder) has less than 256 MiB free, the scan does not relaunch. It parks the job as `user_wait` with `wait_reason: disk_full` and `error: disk_full: <path> has <n> bytes free, the floor is <bytes>`, and uses no recovery. Free space, then `resume` the job.

Three consecutive failed recoveries stop for user judgment. A passing check resets that recovery budget and nothing else. A work, fix or check failure uses only this budget and never a review.

The independent review has its own budget: at most `max_reviews` attempts per cycle, the first included. The scanner reserves each attempt in the locked write that launches the review, so a crash, a reboot, a stall, a lost start or a stale answer cannot hide one: every launched review uses one, whether it ended with a verdict, a malformed answer, an interrupted run or a result that went stale. The one same-session repair of a malformed reply is part of its attempt. An answer with no accepted verdict reviews again directly while an attempt is left and parks at `user_wait/review_exhausted` when none is; it never starts a fix. A `FAIL` or blocking verdict starts a fix only when a later review is still available, so the last permitted review that fails parks at `review_exhausted` without a fix. A result or review basis that changed during a review returns through `check` before the next reserved review. A parsed `PASS`, and the parsed verdict of a review-only job, are delivered whatever the count. Review rounds are numbered across the whole job and never restart, so finding ids `r<round>.F<n>` stay unique. `show <id>` prints `max_reviews`, `reviews_used`, `review_cycle_start` (an index into the retained review records), `review_inflight` (the attempt that holds a reservation its review has not completed) and `retry_not_before`.

Two consecutive substantive failed reviews of one cycle that share a rule id stop the job at `user_wait/root_cause_required` before anything else runs; the root-cause note it asks for is described in [checks](checks.md#finding-records-and-repeated-rules). When the same review also reaches the limit, `root_cause_required` is the reason and the stop notes both.

An explicit `resume` opens a new review cycle at the saved stage: it sets the count to zero, starts the cycle after the last review record, clears the recovery budget and keeps every review record and event. A job that stopped at `review_exhausted` after a failed review resumes into `fix`; one that stopped for answers without a verdict resumes into `review`. `wait` and `approve` keep the cycle. Nothing else resets it: not a passing check, a scan or a restart.

A record written before this accounting has `fix_rounds` and no review fields. It is decoded as it is read and never rewritten. Its review cycle begins at job creation or the latest explicit resume, and its count is the number of unique review launches in that cycle when the events prove a complete history (every review record has its launch, and a running review has its own), without the launches that ended in a terminal 429. The stored `fix_rounds` is a lower bound and never the count itself. A launch-free cycle proves zero use only for a never-started job or an explicitly resumed job that has not executed in the new cycle, when the job is queued or awaiting approval and the fix-round lower bound is zero. Approval-only events and a previous cycle's unit awaiting scanner release do not spend the new cycle. Otherwise, an empty or incomplete cycle history counts as exhausted before another model call: a job at `fix` or `review` parks at `user_wait/review_exhausted` for an explicit resume, and any other stage reaches the same stop at its next review. Its `max_reviews` is 3, whatever the host sets later.

A fresh review uses `docs/review/` profiles and the existing verdict JSON contract. Fingerprints and review basis are computed by code; changing either invalidates the earlier pass.

```sh
taskwatch --root <root> wait <id> --reason "Unapproved external write"
taskwatch --root <root> approve <id> --reason "approval-message-2" --scope "Exact approved scope"
taskwatch --root <root> resume <id> --reason "Approved a new fix cycle"
taskwatch --root <root> cancel <id> --reason "User cancelled"
```

Wait and cancel fence late output immediately. The scanner confirms service termination before reusing the slot, target or landing repository reservation. Approval resumes the saved step; an explicit user resume outside landing opens a new review cycle and a new recovery budget. Resuming a landing stop preserves the accepted review and its review budget; a distinct resume entry authorizes only the specific persisted create, CI observation or install retry described in [Automatic landing](#automatic-landing), not new content or another review cycle. Commands do not send messages themselves; scans send results and stop notices. Landing reconciles only its persisted exact external identities; arbitrary worker actions and unknown result deliveries are not reconciled.

`approve --reason` appends `Gate <date>: <reason>` to the approval source; the original approval stays and the worker and reviewer see both. `approve --scope` records a replacement approved scope. Omit it to preserve the original scope, such as an initial approval wait. Do not use a broader scope than the user actually approved.

After checks and review pass, an eligible job with `land: true` enters `land`; all others enter `deliver_pending`. Successful verified landing and install, or no configured hook, then reaches `deliver_pending`, not `done`; see [Delivery](#delivery).

### Rate limits

A terminal HTTP 429 of a model call ends the step without using a recovery or a review. Only the terminal failure counts: the error of the finished prompt (`prompt ended error: 429 ...`) of an RPC session, or an explicit HTTP 429 in the final diagnostic block of a failed print-mode review command's standard error. Retry header lines and a JSON response body may follow that status. A later plain-text failure or a structured error with another status or error type supersedes it. Explicit final or terminal error markers start a new diagnostic even on the same physical line; markers inside JSON response strings do not. A tool output, a prompt or an earlier model fallback that mentions 429 does not count. When the last failure after a fallback is something else, such as an unsupported API, the step fails like any other failure.

The evidence is an integer `retry-after-ms` or a `Retry-After` value in the same diagnostic, either integer seconds or an [HTTP-date](https://www.rfc-editor.org/rfc/rfc9110.html#section-5.6.7). All three HTTP-date formats are accepted: IMF-fixdate, RFC 850 and asctime; asctime's omitted timezone is UTC. The millisecond form is the provider's own, not an RFC fraction. When several hints are present, the latest finite deadline wins, without an arbitrary duration cap. A negative or fractional delay, a malformed integer token, a nonfinite deadline or an unreadable date is not a hint. The diagnostic stays in `error` with anything that looks like a credential masked.

With a deadline, the runner records `retry_not_before` (epoch seconds), keeps the saved stage and returns the review attempt it reserved, and only that attempt: an accepted verdict and the reservation of another attempt are never returned. The scanner confirms the service dead and frees its execution slot, so other jobs run, but a job in a shared target folder keeps its target and no other writer starts there. No scan or reboot launches the step before the deadline. At or after it, the next scan takes a free slot, applies the usual predecessor, approval, cancellation and target gates, and starts exactly one attempt of the saved stage, which clears the deadline. A zero or past deadline is due at the next scan or wake, never sooner; dates before the Unix epoch are stored as the already-due value `0.0`. The wait is not a decision for the owner, so `needs` does not list it, and it adds nothing to the stall time. `wait`, `approve` and `resume` keep a future deadline; only the due launch or `cancel`, which removes the retry for good, clears it. A `user_wait` that still has a deadline shows it in `needs`.

`needs` renders a deadline in UTC when it fits the calendar; a finite deadline outside that range is shown as epoch seconds instead.

Without a usable hint the job stops at `user_wait` with `wait_reason: rate_limit` and no counter used, because nothing guesses a delay. `resume` clears that stop, since it has no deadline.

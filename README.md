# agent-taskwatch

A job runner for approved agent work. It schedules each job, runs a configured agent command, checks the result with plain code, requests an independent review, and delivers the outcome. A person is asked only when judgment is needed.

## What it is not

- It does not import or patch Hermes Agent. Hermes can submit jobs and check what waits through the `taskwatch` command, as any caller can.
- It does not approve work. Every job carries an approval record that the caller asserts; the runner cannot authenticate the conversation it came from.
- It does not isolate what the runner's tools can read or write. Those tools have the user's permissions, and `approval_scope` is a recorded boundary, not operating-system isolation.
- It does not merge or install results by default. Explicit `land: true` on an eligible approved code job enables the deterministic [landing transaction](docs/configuration.md#automatic-landing), including an optional host-owned pinned install hook.
- Every check is plain code and never calls a model. Work and fixes use omp RPC by default or an explicit [print worker](docs/configuration.md#worker-execution); independent review separately uses RPC or a print command that answers with a JSON verdict.

## How a job runs

A job is a JSON document with a prompt, an intent file, a spec file, required outputs, a check command, a checklist, and the review profile (`code`, `design`, or `source`). A review-only job names a `review_target` instead and skips work and check.

1. **Submit.** `submit` returns after the job and its copied inputs are durable. The receipt key deduplicates retries.
2. **Work.** `scan` reserves a free parallel slot and starts a transient user systemd service that runs the worker in the job workspace. The job keeps its slot across work, check, review, fixes and opt-in landing.
3. **Check.** The runner runs the job's `check_command` and records its exit code and output. Required outputs, progress, and a nonempty passing checklist must also exist. A model's PASS claim does not replace these.
4. **Review.** A separate reviewer invocation reviews the current result against the intent and spec, using the guidelines in [docs/review](docs/review/README.md). It returns a structured verdict. A FAIL or a blocking finding starts a fix while a later review remains: a cycle runs at most `max_reviews` reviews (1 to 3, default 3), the first included, and then the job stops for user judgment. See [Recovery and decisions](docs/configuration.md#recovery-and-decisions).
5. **Land, when enabled.** Default-off `land: true` routes an accepted eligible code result through reviewed publication, exact-head CI, pinned squash, final-tree verification and an optional frozen host install hook. The caller asserts publication/merge/install authority; PASS does not grant it. Stops retain transaction identity for explicit resume or cancellation; see [Automatic landing](docs/configuration.md#automatic-landing).
6. **Delivery.** A nonlanding job that passes check and review, or a successfully landed job, reaches `deliver_pending`. The next `scan` runs the configured `deliver_command`. Only a recorded success makes the job `done`, and it is never sent again. Any other outcome stops at `user_wait` with `delivery_unknown`; the user can `resume` for one new send or `cancel`.

Jobs that share a target folder cannot overlap; code jobs each have their own worktree and can, except landing serializes the canonical git common directory through installation. Approval and user-judgment waits release slots only after service death is confirmed; ambiguous old services and inherited orphan locks keep landing reservations. Dead runs, stalls, and reboot interruptions are recovered after the old service is known to have stopped; three failed recoveries in a row stop for user judgment. A terminal HTTP 429 waits for its evidenced retry time without using a recovery or a review ([Rate limits](docs/configuration.md#rate-limits)).

Details: [checks](docs/checks.md) and [configuration](docs/configuration.md).

## Job kinds and chains

A kind is a label over fields a job already has; there is no `kind` field. `review: source` is a research job, `review: design` a design job, `review: code` an implement job, and a job with a `review_target` is a review. Every job with a work stage writes a decision brief, `taskwatch brief <job>` prints it, and `taskwatch needs` lists what waits for you.

A design job lists the choices that are yours to make (language, runtime, packages, layout, an existing tool) as picks with a recommended option; it never asks for permission, deploy or publish. Chain jobs with `predecessors`; a job that needs your decision on an earlier result carries `approval_required` and opens only after every predecessor is `done`. See [chains and gates](docs/configuration.md#chains-and-gates) and [kinds](docs/kinds/brief.md).

## Work folder lifecycle

A `code` job works in `state/jobs/<id>/worktree` on its own branch, started from a freshly fetched `origin/<default branch>`. Its checklist and progress files live in the job folder, outside the result. When the job reaches `done` or `cancelled`, the scan removes a clean worktree, keeps a dirty one and records why, and deletes the branch only after it is merged or squash merged into `origin/<default branch>`. Job folders, records, reviews, and logs are never deleted. See [code job work folders](docs/configuration.md#code-job-work-folders).

## Install

Requires Linux with user systemd, Python 3.11 or later and git. RPC execution requires omp; a [fully print configuration](docs/configuration.md#standalone-print-configurations) needs only its configured commands. There are no runtime dependencies. macOS and Windows are not supported. Opt-in landing additionally requires authenticated `gh` access to a supported GitHub repository; nonlanding jobs need neither `gh` nor an install hook.

From a checkout, run `uv sync --locked`, then use `uv run taskwatch`. The examples below assume the virtual environment is active.

1. `taskwatch --root <root> init` writes `<root>/taskwatch/jobs-config.json`. Edit it, including `omp_command` and `deliver_command`; the draft `deliver_command` cannot send.
2. `taskwatch --root <root> init --unit-dir <empty-dir>` stages the scan service and timer without installing them. Install them into `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then `systemctl --user enable --now agent-taskwatch-scan.timer`.
3. Submit jobs with `submit`.

See [installation](docs/configuration.md#installation) for the full steps. To remove, disable and delete the two units; the state directory is kept.

## Configuration

Every key, the submission format, delivery, and recovery are in [docs/configuration.md](docs/configuration.md).

## Commands

```sh
taskwatch --root <root> init [--unit-dir <dir>]    # write config; stage timer units
taskwatch --root <root> submit <job.json>          # store an approved job
taskwatch --root <root> scan                       # schedule job steps once
taskwatch --root <root> run <job> --attempt <n>    # one reserved step; started by scan
taskwatch --root <root> show [<job>]               # print job state
taskwatch --root <root> brief <job>                # print the decision brief, composed on read
taskwatch --root <root> needs [--from <cursor> [--wait]]  # list what needs you; read only
taskwatch --root <root> cancel <job> --reason <text>
taskwatch --root <root> wait <job> --reason <text>   # park the job for approval
taskwatch --root <root> approve <job> --reason <text> [--scope <text>]
taskwatch --root <root> resume <job> --reason <text> # open a new recovery and review cycle
```

`--root` defaults to `HERMES_HOME`, then `~/.hermes`. `brief` exits `2` for an unknown job. `needs --wait` exits `0` when something new needs you, `3` at its `--timeout`, `2` without `--from`. `run` is started by `scan`; do not run it alongside the service.

## Limits

- A delivery success means the command reported acceptance, not that the person read the message.
- A delivery whose outcome is unknown is never resent automatically.
- Landing reconciles its persisted exact push/PR/merge identities, not arbitrary worker actions. Head pinning cannot atomically pin the external merge base; final-tree verification detects a race after merge but cannot undo it. Failed or unknown installs never automatically replay; host wrappers and caller approval assertions remain trust boundaries. See [Automatic landing](docs/configuration.md#automatic-landing).
- Work, fix, and review quality depend on the configured commands and their models; the checks cover only what code can verify.

## More

- [docs/configuration.md](docs/configuration.md): config keys, submission, delivery, recovery, state.
- [docs/checks.md](docs/checks.md): what the job checks verify.
- [docs/principles.md](docs/principles.md): the rules behind the design and their sources.
- [docs/review](docs/review/README.md): review guidelines.
- [docs/review-launch.md](docs/review-launch.md): launch a review through taskwatch or with omp alone, from the same guidelines.
- [docs/omp.md](docs/omp.md): optional native omp client over the installed `taskwatch` command.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)

# Launching a review

One set of review guidelines, [docs/review](review/README.md), applies however a review starts. Three things differ by launch path and are kept out of that folder:

| Layer | Where | What it holds |
| --- | --- | --- |
| Guidelines | `docs/review/` | What makes a result pass: rules, severity, rounds, incomplete reviews. Same for every path. |
| Launch layer | `docs/launch/taskwatch.md`, `docs/launch/taskwatch-print.md`, `docs/launch/omp-alone.md` | Verdict format, who counts rounds, tool restrictions, where inputs come from. |
| Agent definition | an omp agent file, for example [examples/omp/reviewer.md](../examples/omp/reviewer.md) | Who the reviewer is: tools, allowed sub-agents, model, and a short static procedure. |

The launcher, `python -m taskwatch.reviewer_launch`, joins the agent definition and the guideline folder when the review starts. It copies no rule text, so editing a guideline file changes the next review.

## What the launcher builds

From the agent file's front matter:

- `tools` is required. It becomes `--tools`; `tools: []` becomes `--no-tools`, because omitting the option would give omp's default tools, `edit` and `write` included. `task` is added when the agent may spawn anything, because spawning needs it.
- `model` becomes `--model` (the first entry of a list); `thinking-level` becomes `--thinking`.
- `spawns` is the list of sub-agents the review may use (`"*"`: any). It is enforced inside omp, not by the prompt: see below.

The agent body, then every guideline file named by `--review` (the README and common rules, then the review's file; all of them when `--review` is omitted), then the optional `--layer` file become one file passed as `--append-system-prompt`. `autoloadSkills` and the nested `output` schema are ignored: omp does not apply an agent's output schema to a command-line session, so the verdict format comes from the launch layer.

The overlay also sets `async.enabled: false`. With async on, `omp -p` ends before a background sub-agent reports, and the review has no verdict.

The files are written under a name that holds their content hash (default `$TMPDIR/taskwatch-reviewer/`; `--work-dir` moves it), so a rerun with unchanged sources reuses them. Options the launcher does not know pass through to omp *before* its own options, so the overlay it builds is the last one omp reads. Pass-through `--tools`, `--no-tools`, `--config`, `--append-system-prompt`, `--system-prompt`, `-e`, `--extension`, `--hook`, `--plugin-dir` and `--trusted-extension` are refused.

Enforcement. `--tools` alone does not limit a command-line session: omp still adds custom tools, MCP servers and plugin agents it discovers, and listing the places to look is never complete. So the launcher generates `enforce.mjs` next to the prompt and loads it with `--trusted-extension`, which makes it the only extension. Inside omp it blocks every call of a tool outside the list (`tool_call`, main session only) and every spawn of an agent outside `spawns` (`before_subagent_spawn`), whatever source the tool or agent came from. A blocked call returns `tool X is not in the reviewer's tool list` or `agent X is not in the reviewer's spawns` to the model. What it does not cover: the tools of sub-agents you allow (they follow their own definitions), and a tool whose name equals an allowed name.

## Through taskwatch

Set the review step's command in `jobs-config.json`:

```json
"review_command": [
  "python3", "-m", "taskwatch.reviewer_launch",
  "--omp", "/usr/local/bin/omp",
  "--agent", "/srv/example/reviewer.md",
  "--guidelines", "{guidelines}", "--review", "{review}"
]
```

`{guidelines}` is the job's own copy of the guidelines, taken when the job first ran, so the basis of the review cannot change under a job. A new job reads the current files. The runner adds `--mode rpc --no-ui`. The message it sends holds the intent, the spec, the target, the round, and `docs/launch/taskwatch.md`.

Submit a [review-only job](configuration.md#review-only-jobs) to review something that already exists.

## Through taskwatch with another tool

With `"review_mode": "print"` the review command is Codex, Claude Code, Gemini CLI or any program that reads the message on its standard input and prints one JSON verdict. Setup, the three examples and how the verdict is found are in [Reviewer other than omp](configuration.md#reviewer-other-than-omp). The launcher above is not used: it builds an omp command.

### Print mode limits

taskwatch starts the command and reads what it prints; it does not sit between the tool and its files.

- It cannot restrict the tools of the other command. The reviewer is read-only only as far as the flags you gave the tool (`--sandbox read-only`, `--permission-mode plan`, `--approval-mode plan`) hold. Nothing here enforces `tools` or `spawns` as the launcher does for omp.
- The review guidelines are named in the message as files; the tool must be able to read them. There is no `--append-system-prompt`, so a tool that cannot read the job folder reviews without the rules.
- There is no session: no same-session repair of a malformed reply and no resume after an interruption. A reply that does not match the schema costs a round, and an interrupted review starts again.
- The evidence refresh every 15 seconds proves only that the process lives. A tool that hangs is stopped after `rpc_timeout` seconds.
- The model, effort and sub-agents are the tool's own settings, not taskwatch's.

## With omp alone

Run the same launcher by hand, with the other launch layer:

```sh
python3 -m taskwatch.reviewer_launch \
  --agent examples/omp/reviewer.md \
  --guidelines docs/review --review code \
  --layer docs/launch/omp-alone.md \
  -p "Round 1. Review commit <sha> of the repository in the current directory against the intent in intent.md and the spec in spec.md."
```

`--print-command` prints the `omp` command instead of running it, so you can read it or run `omp` yourself. `--guidelines` defaults to the folder installed with the package.

Both paths receive the same guideline text. They differ only in the launch layer and in the message.

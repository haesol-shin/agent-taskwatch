## Kind: design (review `design`)

A design job decides how something should be built before it is built, so the owner can choose between real options.

- Inputs: the intent, the spec with the template headings, and the result of any predecessor (for example a research result), listed in the message. When the spec or a work input lists the owner's other repositories and rule files (the conventions to share), read them.
- Result: the files named in `outputs`, usually `design.md`, and always `choices.json`, which must be one of the `outputs` so the review covers it.
- Check: the job's `check_command` (heading and size checks), the required files and the checklist, the `choices.json` check below, then the brief check.
- Review: the common rules, `design.md` and the `design_check` of the verdict line.
- Brief: `Choices made` lists what the design decided and marks every pick `awaiting owner`: the owner answers later, at the gate on the next job, so this job never needs an answer.

### What a pick is

A pick is an implementation choice that the owner decides: language, runtime and its version, dependency manager, packages, layout, conventions shared with the owner's other repositories, and whether an existing tool already does the job. A pick is never an approval request. Permission, deploy, publish, spending and scope belong to the approval scope and the approval gate; a design job never asks for them in `picks`.

A candidate is a pick only if all four tests hold. Otherwise it goes to `decided` with a reason and a source.

- T1 Not settled: no owner default, no choice quoted in the intent or the approval source, no convention in the target repository that already answers it. If the repository answers, follow it and record the file and line.
- T2 A real fork: at least two workable options that differ in something the owner can observe or pay for later (cost, lock-in, effect on other repositories or users, data, security).
- T3 Hard to undo or wide: reversing it costs more than one small job, or it reaches another repository, a user or an external account.
- T4 Evidence does not decide: the agent cannot settle it from sources; it is preference or risk appetite.

At most 5 picks. When more pass, the first 5 by T3 are picks and the rest go to `decided` with `"deferred": true`.

### Finding the choices

Before listing choices, read: the target repository (`AGENTS.md`, `CONTRIBUTING.md`, the README, the manifest and lock files, CI, lint and test configuration, the layout of three neighbouring files); the owner's other repositories and rule files that the spec or a work input lists; and existing tools (the host's own features, installed tools, package registries, a public search). Record at least three different search terms under `existing_tools`.

### `choices.json`

```
{"picks": [{"id": "python.dep_manager", "question": "...",
            "options": [{"key": "A", "label": "uv", "evidence": "pyproject.toml:3"}],
            "recommended": "A", "why": "...",
            "reversible": "cheap|costly", "affects": "this repo|other repos|users"}],
 "decided": [{"id": "layout.tests", "chosen": "...", "why": "...", "source": "AGENTS.md:12",
              "deferred": false}],
 "existing_tools": [{"need": "...", "searched": ["..."],
                     "found": [{"name": "...", "source": "URL or file:line",
                                "reuse": "yes|no", "why": "..."}]}],
 "defaults_applied": []}
```

The check is plain code. It fails when the file is not a JSON object with the lists `picks`, `decided` and `existing_tools`; there are more than 5 picks; a pick has fewer than 2 options, an option without a key, label or evidence, a `recommended` that is not an option key, no question or why, or a `reversible` other than `cheap` or `costly`; a local `file:line` in an option's evidence or a `decided` source names a file or line that does not exist (a URL is not checked); an id is used twice; a `decided` item lacks `chosen`, `why` or `source`, or its id is not in the `Writer choices` section of an output file; `existing_tools` is empty, an entry lacks `need`, `searched` or `found` (`found: []` is allowed when terms were searched), or fewer than 3 different terms are recorded in total. When `<root>/taskwatch/defaults.json` exists, it is validated against [`docs/kinds/defaults.schema.json`](defaults.schema.json) (`{"version": 1, "defaults": [{"id", "value", "scope": "global" | "<target path>", "source": "<owner words>"}]}`); when several entries match a job's target, the most specific scope wins (a target path beats `global`; a longer matching path beats a shorter one), as in `git config` and Claude Code settings. The check fails when `defaults.json` is malformed, when a `defaults_applied` id is not in `defaults.json`, or when a pick id has an applicable default (T1: settled). Write a default only on the owner's explicit words ("always", "from now on"); `source` holds them. It cannot tell whether a decided item should have been a pick: the reviewer judges each one against T1 to T4.

### Answering

The picks gate sits on the job after the design job, never on the design job itself. `taskwatch brief <design job>` and the delivery message show each pick with its recommended option and end with `reply: ok | <id>=<key|text> ... | free text`. `ok` accepts every recommended option. The owner's reply goes to `approve --reason`, preceded by one line `PICKS: ok` or `PICKS: <id>=<key|text> ...` (`ok` and overrides may be combined; quote a value that has spaces). The reason is appended to the approval source, so the next job and its reviewer see the picks as owner decisions. An implement job after a design job fails its check when its approval source has no `PICKS:` line for a design with picks, when a pick has no answer, or when its `Choices made` does not list every pick and decided id.

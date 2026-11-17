# AGENTS.md

Instructions for coding agents working in this repository.

## Commands

```sh
uv run python scripts/check_fast.py
```

The command runs CI's lint, docs, commit message, Python test and extension checks with the same
arguments, and selects only the test files of changed modules. The commit check reads the range
from the merge base to `HEAD`; the measured slow test, paths it cannot map and the extension
smoke stay in CI. Run it before you report a change as done.

## Rules

- Read [CONTRIBUTING.md](CONTRIBUTING.md) first. It is the source for code style, commits, and pull requests.
- No runtime dependencies. Standard library only.
- Checks never call a model.
- Do not import Hermes. The runner stays standalone.
- Keep the change minimal. Do not refactor unrelated code.
- Tests use synthetic fixtures. Use `.invalid` hosts and placeholder paths, never real accounts, chat ids, or local paths.

## Before you publish

- Ask the maintainer before you post an issue, a comment, or a pull request, and before you merge.
- Search your diff for personal values, tokens, and absolute local paths.
- Add a `CHANGELOG.md` line under `Unreleased` if users can see the change.

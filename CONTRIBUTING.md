# Contributing

Thank you for your interest in agent-taskwatch. The rules are short.

## Before you start

- Open an issue to report a bug or to propose work you are not going to implement yourself.
- If you are going to implement a change yourself, do not open an issue for it first. Open the pull request directly. For a new feature or a larger change, start a discussion in [Discussions](https://github.com/haesol-shin/agent-taskwatch/discussions) before you write code, so the approach is agreed.
- If an issue for the change already exists, link it from the pull request.

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pytest
uv run python scripts/check_review_docs.py
```

CI runs the same commands. A pull request must pass all of them.

## Code style

CI enforces the Ruff rules `E`, `F`, `I`, `C901` (max complexity 15) and line length 100. The rest is the recommended style:

- Python 3.11 or later, no runtime dependencies.
- Checks are pure functions. Config and state are frozen dataclasses. No inheritance.
- Keep changes minimal and focused. One function does one thing.
- Write tests that fail when the behavior they cover breaks.
- Comments explain why, not what.

## Commits

CI does not check commit messages. The recommended form explains why the code is the way it is; the diff already shows what changed.

- Title: [Conventional Commits](https://www.conventionalcommits.org/), `type(scope): summary` in the imperative, in English, 72 characters or fewer (about 50 is best). No trailers.
- Body: the problem or the reason for the change, with lines of 72 characters or fewer. A rejected alternative (`Rejected: ...`) helps when another approach looks more natural.
- Avoid listing files, restating the diff, describing the review process, or pasting test results. Those go in the pull request.
- Maintainers who run jobs with taskwatch may enable stricter commit checks; see [docs/checks.md](docs/checks.md).

Example:

```
fix(jobs): reject a review of an older result

A pass on an earlier commit could deliver a result that changed later.
The runner compares the reviewed fingerprint with the current one.
Rejected: warn only, because delivery would already go ahead.
```

## Pull requests

Pull request titles should use the same `type(scope): summary` format as commit titles.

The pull request body is the evidence for merging this change now: tests run, CI, review, linked issue if any, and known limits. It stays on GitHub and is reached through the `#number` in the commit title.

- Fill in the pull request template. If an issue exists, link it with `Closes #`.
- Add one line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md) when the change is visible to users.
- Do not commit secrets, personal values, or local paths. Use placeholders in examples.
- Merging:
  - Every pull request is squash merged, so `main` has one commit per pull request. Write the squash subject as the pull request title plus `(#number)` and the body by the commit form above, replacing GitHub's commit list. Merge commits and rebase merging are off: a pull request is one change, and its step commits stay on the pull request page.
  - Split a large change into pull requests that can each be reviewed and merged on their own ([Google, Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)).
  - Merge only after the maintainer approves the current head, and pin it: `gh pr merge <number> --match-head-commit <approved SHA>`. A new commit after approval needs a new approval.
  - Follow the [review-bot severity policy](docs/configuration.md#review-bots-policy) for teammate PRs too, including bot-only reviews. While review opportunities remain, address all priorities; at the agreed limit, retain lower-priority leftovers in one linked followup and name them in the PR handoff. The maintainer approves reasons for leaving bot findings unfixed. Contributors do not need taskwatch.

## Using AI tools

AI tools are welcome. You are responsible for what you submit:

- Check the result and attach evidence (test output, logs, or the review that found no issues).
- Make sure you can explain the change and maintain it.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

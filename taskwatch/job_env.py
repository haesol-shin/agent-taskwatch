"""The environment every process of a job runs in.

One helper builds it from the job config's `env` object, so the omp worker, the check command,
the reviewer command and the git commands the runner starts all see the same values.
"""

import os
from collections.abc import Mapping
from pathlib import Path


def validate_env(value: object) -> dict[str, str]:
    """The config's `env` object as a plain dict, or a ValueError naming the problem."""
    if not isinstance(value, dict):
        raise ValueError("env must be an object of string keys and string values")
    for key, item in value.items():
        if not isinstance(key, str) or not key or "=" in key or "\0" in key:
            raise ValueError(f"env key must be a nonempty string without '=': {key!r}")
        if not isinstance(item, str) or "\0" in item:
            raise ValueError(f"env value must be a string: {key}")
    return dict(value)


def _expand_entry(entry: str, home: str) -> str:
    entry = entry.replace("${HOME}", home).replace("$HOME", home)
    if entry == "~" or entry.startswith("~/"):
        entry = home + entry[1:]
    return entry


def _expand_path(value: str, inherited: str, home: str) -> str:
    entries = []
    for entry in value.split(os.pathsep):
        if entry in ("$PATH", "${PATH}"):
            if inherited:
                entries.append(inherited)
        else:
            entries.append(_expand_entry(entry, home))
    return os.pathsep.join(entries)


def job_environment(
    config_env: Mapping[str, str],
    base: Mapping[str, str] | None = None,
    *,
    private_tmp: bool = False,
) -> dict:
    """The inherited environment plus the configured values; no shell runs.

    In `PATH`, `~` and `$HOME` expand and `$PATH` stands for the inherited value. Other values
    are used as written. With `private_tmp`, `TMPDIR` is `/tmp` because the step sees its own
    folder there, so a configured `TMPDIR` cannot point temporary files at a shared folder.
    """
    env = dict(os.environ if base is None else base)
    home = env.get("HOME") or str(Path.home())
    for key, value in config_env.items():
        env[key] = _expand_path(value, env.get("PATH", ""), home) if key == "PATH" else value
    if private_tmp:
        env["TMPDIR"] = "/tmp"
    return env

/**
 * `bun run smoke`: real-load smoke launcher. Bun resolves executables from the PATH it started
 * with, so the stubs below must exist before the process that loads the extension starts. This
 * file prepares one disposable directory (git-ignored, removed afterwards), then runs
 * smoke-run.ts with HOME, the omp profile and PATH pointing into it.
 *
 * Prerequisites (the smoke fails, never skips, without them): `bun install --frozen-lockfile`
 * in this directory, `taskwatch` on PATH and a Linux /proc. No model, network or live state.
 */
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { delimiter, join } from "node:path";
import { layout } from "./smoke-layout";

const realCli = Bun.which("taskwatch");
if (!realCli) throw new Error("smoke prerequisite missing: `taskwatch` is not on PATH");
if (!(await Bun.file(join(import.meta.dir, "node_modules/@oh-my-pi/pi-coding-agent/package.json")).exists())) {
  throw new Error("smoke prerequisite missing: run `bun install --frozen-lockfile` in this directory");
}

await mkdir(join(import.meta.dir, ".smoke"), { recursive: true });
const tmp = await mkdtemp(join(import.meta.dir, ".smoke", "run-"));
const dirs = layout(tmp);
let code = 1;
try {
  for (const dir of Object.values(dirs)) await mkdir(dir, { recursive: true });
  // systemd-run stub: the submit wake is recorded and never scheduled.
  await writeFile(join(dirs.bin, "systemd-run"), `#!/bin/sh\necho "$@" >> '${tmp}/systemd-run.log'\nexit 0\n`, { mode: 0o755 });
  // Proxy: forwards every call to the installed CLI, with the stub first on PATH. Only while
  // armed does it hang right after a durable submit, so a test can lose that submit's reply.
  await writeFile(
    join(dirs.proxy, "taskwatch"),
    `#!/bin/sh
PATH='${dirs.bin}':"$PATH"
export PATH
printf '%s\\n' "$3" >> '${tmp}/calls.log'
if [ "$3" = submit ] && [ -e '${tmp}/arm' ]; then
  '${realCli}' "$@" >/dev/null 2>&1 || exit 1
  : > '${tmp}/committed'
  exec sleep 600.123
fi
exec '${realCli}' "$@"
`,
    { mode: 0o755 },
  );
  const env = {
    HOME: dirs.home,
    PI_CODING_AGENT_DIR: dirs.profile,
    PATH: [dirs.proxy, dirs.bin, process.env.PATH ?? ""].join(delimiter),
  };
  const child = Bun.spawn([process.execPath, join(import.meta.dir, "smoke-run.ts"), tmp], { env, stdout: "inherit", stderr: "inherit" });
  code = await child.exited;
} finally {
  await rm(tmp, { recursive: true, force: true });
}
process.exit(code);

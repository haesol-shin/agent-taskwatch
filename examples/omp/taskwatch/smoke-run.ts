/**
 * Body of the real-load smoke, started by smoke.ts with isolated HOME, omp profile and PATH.
 * It loads index.ts through the public `loadExtensions`, initializes the public `ExtensionRunner`
 * with synthetic actions and runs the registered handlers with the loader's own `pi.exec`
 * against the installed `taskwatch`. No model, network, live profile or live state.
 */
import { readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { AuthStorage, ModelRegistry, SessionManager } from "@oh-my-pi/pi-coding-agent";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import type { Details } from "./client";
import { layout } from "./smoke-layout";

const HERE = import.meta.dir;
const tmp = process.argv[2];
if (!tmp) throw new Error("smoke-run.ts is started by smoke.ts");
const dirs = layout(tmp);

let steps = 0;
function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`smoke failed: ${message}`);
  steps++;
}
async function exists(path: string): Promise<boolean> {
  return stat(path).then(() => true, () => false);
}
async function until(condition: () => Promise<boolean>, message: string): Promise<void> {
  for (let attempt = 0; attempt < 400; attempt++) {
    if (await condition()) return;
    await Bun.sleep(25);
  }
  throw new Error(`smoke failed: timed out waiting for ${message}`);
}
/** Live processes of this smoke (its root in the command line, or the hung proxy) containing every needle. */
async function processes(...needles: string[]): Promise<string[]> {
  const found: string[] = [];
  for (const pid of (await readdir("/proc")).filter(name => /^[0-9]+$/.test(name))) {
    const line = await readFile(`/proc/${pid}/cmdline`, "utf8").then(text => text.split("\0").join(" "), () => "");
    if ((line.includes(dirs.root) || line.includes("sleep 600.123")) && needles.every(needle => line.includes(needle))) found.push(line);
  }
  return found;
}
const jobCount = async () => (await readdir(join(dirs.root, "taskwatch", "state", "jobs"))).length;

async function writeJob(name: string, receipt: string): Promise<string> {
  const path = join(dirs.inputs, name);
  await writeFile(join(dirs.inputs, "intent.md"), "Synthetic intent for the smoke.\n");
  await writeFile(join(dirs.inputs, "spec.md"), "Synthetic spec for the smoke.\n");
  await writeFile(
    path,
    JSON.stringify({
      receipt_key: receipt,
      approval_source: "approval-smoke.invalid",
      approval_scope: "Create local deliverables only",
      prompt: "Write the synthetic deliverable.",
      review: "source",
      target: dirs.target,
      intent: join(dirs.inputs, "intent.md"),
      spec: join(dirs.inputs, "spec.md"),
      outputs: ["result.md"],
      check_command: ["sh", "check.sh"],
      checklist: "checklist.json",
      progress: "progress.md",
      approval_required: false,
    }),
  );
  return path;
}

/** Load the real extension and initialize the real runner; `flag` is the --taskwatch-root value. */
async function boot(flag: string | undefined) {
  const loaded = await loadExtensions([join(HERE, "index.ts")], dirs.work);
  check(loaded.errors.length === 0, `extension load errors: ${JSON.stringify(loaded.errors)}`);
  const extension = loaded.extensions[0];
  check(loaded.extensions.length === 1 && extension, "exactly one extension loaded");
  check(
    [...extension.tools.keys()].sort().join() === "taskwatch_needs,taskwatch_show,taskwatch_submit",
    `registered tools: ${[...extension.tools.keys()].join()}`,
  );
  check(extension.flags.get("taskwatch-root")?.type === "string", "string flag taskwatch-root registered");
  check(extension.commands.size === 0 && extension.shortcuts.size === 0, "no slash command or shortcut");
  if (flag !== undefined) loaded.runtime.flagValues.set("taskwatch-root", flag);
  const sent: string[] = [];
  const auth = await AuthStorage.create(join(dirs.profile, "auth.db"));
  const registry = new ModelRegistry(auth, join(dirs.profile, "models.yml"), { cacheDbPath: join(dirs.profile, "models.db") });
  const runner = new ExtensionRunner(loaded.extensions, loaded.runtime, dirs.work, SessionManager.inMemory(dirs.work), registry);
  const unavailable = (name: string) => () => {
    throw new Error(`synthetic action ${name} is not available in the smoke`);
  };
  runner.initialize(
    {
      sendMessage: () => void sent.push("sendMessage"),
      sendUserMessage: () => void sent.push("sendUserMessage"),
      appendEntry: () => void sent.push("appendEntry"),
      setLabel: () => undefined,
      getActiveTools: () => [],
      getAllTools: () => [],
      setActiveTools: async () => undefined,
      getCommands: () => [],
      setModel: async () => false,
      getThinkingLevel: () => undefined,
      setThinkingLevel: unavailable("setThinkingLevel"),
      getSessionName: () => undefined,
      setSessionName: async () => undefined,
    },
    {
      getModel: () => undefined,
      isIdle: () => true,
      abort: () => undefined,
      hasPendingMessages: () => false,
      shutdown: () => undefined,
      getContextUsage: () => undefined,
      compact: async () => undefined,
      getSystemPrompt: () => [],
    },
  );
  const tools = new Map(runner.getAllRegisteredTools().map(tool => [tool.definition.name, tool.definition]));
  const run = async (name: string, params: unknown, signal?: AbortSignal) => {
    const tool = tools.get(name);
    check(tool, `${name} is registered with the runner`);
    const result = await tool.execute(`smoke-${name}`, params as never, signal, undefined, runner.createContext());
    return { text: result.content.map(block => (block.type === "text" ? block.text : "")).join(""), details: result.details as Details, isError: result.isError };
  };
  return { runner, run, sent };
}

try {
  // Missing root: a visible configuration error before any subprocess.
  const unconfigured = await boot(undefined);
  for (const [name, params] of [["taskwatch_show", { job_id: "x" }], ["taskwatch_needs", {}]] as const) {
    const result = await unconfigured.run(name, params);
    check(result.details.state === "error" && result.isError && result.text.includes("--taskwatch-root"), `${name} without root is an error`);
  }
  check(!(await exists(join(tmp, "calls.log"))), "no taskwatch process ran without a root");
  const relative = await boot("relative/root");
  check((await relative.run("taskwatch_needs", {})).details.state === "error", "relative root is an error");
  check(!(await exists(join(tmp, "calls.log"))), "no taskwatch process ran for a relative root");

  const { runner, run, sent } = await boot(dirs.root);

  // Missing CLI: a child with a PATH that has no taskwatch, through the same real loader.
  const child = Bun.spawn([process.execPath, join(HERE, "smoke-missing-cli.ts"), join(HERE, "index.ts"), dirs.root, dirs.work], {
    env: { HOME: dirs.home, PI_CODING_AGENT_DIR: dirs.profile, PATH: dirs.empty },
    stdout: "pipe",
    stderr: "pipe",
  });
  const [childOut, childErr] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  check(child.exitCode === 0, `missing-CLI child ran: ${childErr}`);
  const noCli = JSON.parse(childOut) as { details: Details; isError?: boolean; text: { text: string }[] };
  check(noCli.details.state === "error" && noCli.isError && /not found on PATH/.test(noCli.text[0]?.text ?? ""), `missing CLI is a clear error: ${childOut}`);

  // Missing, invalid and non-approved files fail before the CLI is asked.
  check((await run("taskwatch_submit", { job_file: join(dirs.inputs, "absent.json") })).details.state === "error", "missing job file is an error");
  await writeFile(join(dirs.inputs, "invalid.json"), "{ not json");
  check((await run("taskwatch_submit", { job_file: join(dirs.inputs, "invalid.json") })).details.state === "error", "invalid JSON is an error");
  check((await run("taskwatch_submit", { job_file: "relative.json" })).details.state === "error", "relative job file is an error");
  check(!(await exists(join(tmp, "calls.log"))), "file failures started no taskwatch process");

  // Submit, replay and show through the real CLI.
  const jobFile = await writeJob("job-1.json", "approval-smoke.invalid:job-1");
  const submitted = await run("taskwatch_submit", { job_file: jobFile });
  check(submitted.details.state === "ok" && submitted.details.job_id, `submit succeeds: ${submitted.text}`);
  const id = submitted.details.job_id;
  check(submitted.details.receipt_key === "approval-smoke.invalid:job-1", "submit keeps the receipt");
  check(await exists(join(tmp, "systemd-run.log")), "the submit wake went to the stub, not a real service");
  const replay = await run("taskwatch_submit", { job_file: jobFile });
  check(replay.details.job_id === id && (await jobCount()) === 1, "replaying the same file returns the original id and one job");
  const shown = await run("taskwatch_show", { job_id: id });
  check(shown.details.state === "ok" && shown.details.job_id === id, "show returns the same id");
  check(shown.details.receipt_key === "approval-smoke.invalid:job-1" && shown.details.record?.stage === "queued", "show returns receipt and stage");
  const unknown = await run("taskwatch_show", { job_id: "0".repeat(32) });
  check(unknown.details.state === "error" && unknown.isError, "unknown job id is an error");

  // Cursor and a short real quiet wait that ends at the CLI timeout with the cursor intact.
  const listed = await run("taskwatch_needs", {});
  const cursor = listed.details.cursor;
  check(listed.details.state === "ok" && cursor && /^[0-9]+$/.test(cursor), "needs returns a decimal cursor");
  check(BigInt(cursor) > BigInt(Number.MAX_SAFE_INTEGER), "the real cursor exceeds 2^53 and survives as a string");
  const started = Date.now();
  const waited = await run("taskwatch_needs", { cursor, wait: true, timeout_seconds: 1 });
  check(waited.details.state === "timeout" && waited.details.cursor === cursor, "a bounded wait ends as timeout with the cursor intact");
  check(waited.details.process?.code === 3 && Date.now() - started < 15_000, "the timeout is the CLI's exit 3 within its bound");
  check(!waited.isError, "a bounded timeout is an ordinary result");

  // Interrupt a submit after its durable commit; the replay reconciles to one job.
  const second = await writeJob("job-2.json", "approval-smoke.invalid:job-2");
  await writeFile(join(tmp, "arm"), "");
  const controller = new AbortController();
  const interrupted = run("taskwatch_submit", { job_file: second }, controller.signal);
  await until(() => exists(join(tmp, "committed")), "the durable commit of the interrupted submit");
  check((await jobCount()) === 2, "the second job is durable before the interruption");
  check((await processes("sleep 600.123")).length === 1, "the lost-reply submit process is still hanging before the abort");
  controller.abort();
  const lost = await interrupted;
  await rm(join(tmp, "arm"));
  check((await processes("sleep 600.123")).length === 0, "abort ended the hung submit process");
  check(lost.details.state === "unknown" && lost.details.receipt_key === "approval-smoke.invalid:job-2", "interrupted submit reports unknown with its receipt");
  check(!lost.details.job_id && !/cancelled the job/i.test(lost.text), "no fabricated id or cancellation claim");
  const reconciled = await run("taskwatch_submit", { job_file: second });
  check(reconciled.details.state === "ok" && reconciled.details.job_id && reconciled.details.job_id !== id, "replay reconciles the interrupted submit");
  check((await jobCount()) === 2, "the interrupted submit produced exactly one durable job");
  const again = await run("taskwatch_submit", { job_file: second });
  check(again.details.job_id === reconciled.details.job_id && (await jobCount()) === 2, "another replay stays idempotent");
  check((await run("taskwatch_show", { job_id: reconciled.details.job_id })).details.record?.receipt_key === "approval-smoke.invalid:job-2", "show confirms the reconciled receipt");

  // Abort, replacement and lifecycle shutdown leave no child wait and no durable change.
  const abortWait = new AbortController();
  const firstWait = run("taskwatch_needs", { cursor, wait: true, timeout_seconds: 300 }, abortWait.signal);
  await until(async () => (await processes("needs", "--wait")).length > 0, "the first real wait process");
  abortWait.abort();
  const aborted = await firstWait;
  check(aborted.details.state === "cancelled" && aborted.details.cursor === cursor, "an aborted wait is cancelled with its cursor");
  check((await processes("needs", "--wait")).length === 0, "abort ended the wait process");
  check((await run("taskwatch_show", { job_id: id })).details.record?.stage === "queued", "the durable job survives client abort");

  const waitA = run("taskwatch_needs", { cursor, wait: true, timeout_seconds: 300 });
  await until(async () => (await processes("needs", "--wait")).length > 0, "wait A process");
  let peak = 0;
  let samples = 0;
  let stop = false;
  const sampler = (async () => {
    while (!stop) {
      peak = Math.max(peak, (await processes("needs", "--wait")).length);
      samples++;
      await Bun.sleep(5);
    }
  })();
  const waitB = run("taskwatch_needs", { cursor, wait: true, timeout_seconds: 300 });
  check((await waitA).details.state === "cancelled", "a replacement wait cancels the old wait");
  await until(async () => (await processes("needs", "--wait")).length > 0, "wait B process");
  stop = true;
  await sampler;
  check(samples > 0 && peak <= 1 && (await processes("needs", "--wait")).length === 1, `at most one wait process at a time (peak ${peak}, samples ${samples})`);
  await runner.emit({ type: "session_switch", reason: "new", previousSessionFile: undefined });
  const switched = await waitB;
  check(switched.details.state === "cancelled" && switched.details.cause === "session", "session_switch cancels and fences the wait");
  check((await processes("needs", "--wait")).length === 0, "session_switch left no wait process");

  const lastWait = run("taskwatch_needs", { cursor, wait: true, timeout_seconds: 300 });
  await until(async () => (await processes("needs", "--wait")).length > 0, "the final wait process");
  await runner.emit({ type: "session_shutdown" });
  check((await lastWait).details.cause === "session" && (await processes("needs", "--wait")).length === 0, "session_shutdown leaves no child wait");
  check(sent.length === 0, "no message, turn or entry was sent to the session");

  const calls = (await readFile(join(tmp, "calls.log"), "utf8")).split("\n").filter(Boolean);
  check(calls.every(call => ["submit", "show", "needs"].includes(call)), `only submit, show and needs ran: ${[...new Set(calls)].join()}`);
  check((await processes()).length === 0, "no taskwatch process of the smoke root remains");
  console.log(`smoke passed: ${steps} checks`);
} finally {
  await rm(join(tmp, "arm"), { force: true });
}

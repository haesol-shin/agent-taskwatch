import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { getEventListeners } from "node:events";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { bound, cursorOf, type Exec, type ProcessResult, TaskwatchClient, type ToolResult } from "./client";

type Options = NonNullable<Parameters<Exec>[2]>;
type Script = (args: string[], options: Options) => ProcessResult | Promise<ProcessResult>;

const ROOT = "/srv/taskwatch-example";
const ok = (stdout: string, stderr = ""): ProcessResult => ({ stdout, stderr, code: 0, killed: false });
const killed: ProcessResult = { stdout: "", stderr: "", code: -1, killed: true };

/** Resolves like the public exec: killed once the signal aborts, after `ticks` microtasks of slow disposal. */
async function untilAborted(signal: AbortSignal | undefined, ticks = 0): Promise<ProcessResult> {
  const { promise, resolve } = Promise.withResolvers<void>();
  if (signal?.aborted) resolve();
  else signal?.addEventListener("abort", () => resolve(), { once: true });
  await promise;
  for (let tick = 0; tick < ticks; tick++) await Promise.resolve();
  return killed;
}

function harness(script: Script, root: () => unknown = () => ROOT) {
  const calls: { command: string; args: string[]; options: Options }[] = [];
  const waiters: { count: number; resolve: () => void }[] = [];
  const exec: Exec = async (command, args, options) => {
    calls.push({ command, args, options: options ?? {} });
    for (const waiter of waiters) if (calls.length >= waiter.count) waiter.resolve();
    return script(args, options ?? {});
  };
  /** Resolves once `count` processes have been launched. */
  const reached = (count: number): Promise<void> => {
    const { promise, resolve } = Promise.withResolvers<void>();
    waiters.push({ count, resolve });
    if (calls.length >= count) resolve();
    return promise;
  };
  return { calls, reached, client: new TaskwatchClient(exec, root) };
}

// Scratch space stays inside the package (git-ignored), never the system temp directory.
const scratch = join(import.meta.dir, ".smoke");
let dir: string;
beforeEach(async () => {
  await mkdir(scratch, { recursive: true });
  dir = await mkdtemp(join(scratch, "unit-"));
});
afterEach(() => rm(dir, { recursive: true, force: true }));

async function jobFile(name: string, receipt: unknown = "approval:job-1"): Promise<string> {
  const path = join(dir, name);
  await writeFile(path, JSON.stringify({ receipt_key: receipt, prompt: "synthetic" }));
  return path;
}

const record = (extra: Record<string, unknown> = {}) => ({ id: "job-id-1", receipt_key: "approval:job-1", stage: "queued", ...extra });
const text = (result: ToolResult) => result.content.map(block => block.text).join("");

describe("argv and root", () => {
  test("runs taskwatch without a shell and passes metacharacter paths as one argv element", async () => {
    const odd = join(dir, "a b; $(touch pwned) 'q' `x`.json");
    await writeFile(odd, JSON.stringify({ receipt_key: "k" }));
    const { client, calls } = harness(() => ok(JSON.stringify(record({ receipt_key: "k" }))));
    const result = await client.submit(odd, undefined, dir);
    expect(result.details.state).toBe("ok");
    expect(calls).toEqual([{ command: "taskwatch", args: ["--root", ROOT, "submit", odd], options: { cwd: dir, signal: expect.any(AbortSignal), timeout: 60_000 } }]);
  });

  test.each([undefined, "", "  ", "relative/root", 3, true])("rejects root %p before any process", async root => {
    const { client, calls } = harness(() => ok("{}"), () => root);
    const results = [
      await client.show("j", undefined, dir),
      await client.needs({}, undefined, dir),
      await client.submit(await jobFile("job.json"), undefined, dir),
    ];
    for (const result of results) {
      expect(result.details.state).toBe("error");
      expect(result.isError).toBe(true);
      expect(text(result)).toContain("--taskwatch-root");
    }
    expect(calls).toEqual([]);
  });

  test("reports a missing executable as an error that names PATH", async () => {
    const client = new TaskwatchClient(async () => { throw new Error('Executable not found in $PATH: "taskwatch"'); }, () => ROOT);
    const result = await client.show("j", undefined, dir);
    expect(result.details).toMatchObject({ state: "error", tool: "show" });
    expect(text(result)).toContain("not found on PATH");
  });
});

describe("submit", () => {
  test.each([
    ["missing receipt", {}],
    ["empty receipt", { receipt_key: " " }],
    ["numeric receipt", { receipt_key: 7 }],
  ])("requires a nonempty receipt_key (%s)", async (_name, body) => {
    const path = join(dir, "x.json");
    await writeFile(path, JSON.stringify(body));
    const { client, calls } = harness(() => ok("{}"));
    expect((await client.submit(path, undefined, dir)).details.state).toBe("error");
    expect(calls).toEqual([]);
  });

  test("rejects relative, missing, invalid and non-object files before running", async () => {
    const bad = join(dir, "bad.json");
    await writeFile(bad, "{not json");
    const list = join(dir, "list.json");
    await writeFile(list, "[]");
    const { client, calls } = harness(() => ok("{}"));
    for (const file of ["job.json", join(dir, "missing.json"), bad, list]) {
      expect((await client.submit(file, undefined, dir)).details.state).toBe("error");
    }
    expect(calls).toEqual([]);
  });

  test("accepts exit 0 with one record of the same receipt and keeps stderr warnings in details", async () => {
    const file = await jobFile("job.json");
    const { client } = harness(() => ok(JSON.stringify(record()), "warning: synthetic"));
    const result = await client.submit(file, undefined, dir);
    expect(result.isError).toBeUndefined();
    expect(result.details).toMatchObject({ state: "ok", job_id: "job-id-1", receipt_key: "approval:job-1" });
    expect(result.details.process).toEqual({ stdout: JSON.stringify(record()), stderr: "warning: synthetic", code: 0, killed: false });
  });

  test("fails visibly when the replay returns another receipt or the taskwatch refuses", async () => {
    const file = await jobFile("job.json");
    const other = harness(() => ok(JSON.stringify(record({ receipt_key: "other" }))));
    const mismatch = await other.client.submit(file, undefined, dir);
    expect(mismatch.details).toMatchObject({ state: "error", receipt_key: "approval:job-1" });
    expect(text(mismatch)).toContain("not approval:job-1");
    const refused = harness(() => ({ stdout: "", stderr: "taskwatch: refused", code: 1, killed: false }));
    const result = await refused.client.submit(file, undefined, dir);
    expect(result.details.state).toBe("error");
    expect(text(result)).toContain("refused");
  });

  test.each([
    ["trailing text", `${JSON.stringify(record())} extra`],
    ["an array", "[]"],
    ["an empty id", JSON.stringify(record({ id: "" }))],
    ["no output", ""],
  ])("reports an unknown outcome with the receipt for a %s reply", async (_name, stdout) => {
    const file = await jobFile("job.json");
    const { client } = harness(() => ok(stdout));
    const result = await client.submit(file, undefined, dir);
    expect(result.details).toMatchObject({ state: "unknown", cause: "malformed", receipt_key: "approval:job-1" });
    expect(result.details.job_id).toBeUndefined();
    expect(text(result)).toContain("not cancelled");
  });

  test("reconciles an interrupted submit by replaying the same argv: one durable job", async () => {
    const file = await jobFile("job.json");
    const store = new Map<string, Record<string, unknown>>();
    let commits = 0;
    const { client, calls, reached } = harness(async (args, options) => {
      const body = JSON.parse(await Bun.file(args[3]!).text()) as { receipt_key: string };
      if (!store.has(body.receipt_key)) {
        store.set(body.receipt_key, record({ id: `job-${++commits}` }));
        // The commit is durable; the reply is lost to an interruption.
        return commits === 1 ? untilAborted(options.signal) : ok(JSON.stringify(store.get(body.receipt_key)));
      }
      return ok(JSON.stringify(store.get(body.receipt_key)));
    });
    const controller = new AbortController();
    const first = client.submit(file, controller.signal, dir);
    await reached(1);
    controller.abort();
    const interrupted = await first;
    expect(interrupted.details).toMatchObject({ state: "unknown", cause: "cancelled", receipt_key: "approval:job-1" });
    expect(interrupted.details.job_id).toBeUndefined();
    expect(text(interrupted)).toContain("not cancelled");
    const replay = await client.submit(file, undefined, dir);
    expect(replay.details).toMatchObject({ state: "ok", job_id: "job-1" });
    expect(store.size).toBe(1);
    expect(commits).toBe(1);
    expect(calls[1]!.args).toEqual(calls[0]!.args);
    expect(calls.every(call => call.args.includes("submit"))).toBe(true);
  });

  test("a client deadline is an unknown outcome, not a rollback", async () => {
    const file = await jobFile("job.json");
    const { client } = harness(() => killed);
    const result = await client.submit(file, undefined, dir);
    expect(result.details).toMatchObject({ state: "unknown", cause: "timeout", receipt_key: "approval:job-1" });
  });

  test("an aborted signal never starts a process", async () => {
    const file = await jobFile("job.json");
    const { client, calls } = harness(() => ok("{}"));
    const result = await client.submit(file, AbortSignal.abort(), dir);
    expect(result.details).toMatchObject({ state: "cancelled", receipt_key: "approval:job-1" });
    expect(calls).toEqual([]);
  });
});

describe("show", () => {
  test("returns the authoritative record and keeps stderr warnings aside", async () => {
    const full = record({ activity: "work 3m; last progress 1m ago: é", extra: { nested: [1] } });
    const { client, calls } = harness(() => ok(JSON.stringify(full), "warning"));
    const result = await client.show("job-id-1", undefined, dir);
    expect(calls[0]!.args).toEqual(["--root", ROOT, "show", "job-id-1"]);
    expect(result.details).toMatchObject({ state: "ok", job_id: "job-id-1", receipt_key: "approval:job-1", record: full });
    expect(text(result)).toBe("id: job-id-1\nreceipt_key: approval:job-1\nstage: queued\nactivity: work 3m; last progress 1m ago: é");
  });

  test.each([
    ["another id", JSON.stringify(record({ id: "other" }))],
    ["trailing output", `${JSON.stringify(record())}\n{}`],
    ["an array", "[]"],
    ["text", "show: ok"],
    ["a missing id", JSON.stringify({ stage: "queued" })],
  ])("treats %s as an error", async (_name, stdout) => {
    const { client } = harness(() => ok(stdout));
    const result = await client.show("job-id-1", undefined, dir);
    expect(result.details.state).toBe("error");
    expect(result.isError).toBe(true);
  });

  test("surfaces unknown jobs, nonzero exits, empty ids and killed processes", async () => {
    const unknown = harness(() => ({ stdout: "", stderr: "taskwatch: 'nope'", code: 1, killed: false }));
    const result = await unknown.client.show("nope", undefined, dir);
    expect(result.details.state).toBe("error");
    expect(text(result)).toContain("'nope'");
    expect((await harness(() => ok("{}")).client.show("  ", undefined, dir)).details.state).toBe("error");
    const deadline = await harness(() => killed).client.show("j", undefined, dir);
    expect(deadline.details).toMatchObject({ state: "error", cause: "timeout" });
    expect(deadline.isError).toBe(true);
    const hungWait = await harness(() => killed).client.needs({ cursor: "4", wait: true }, undefined, dir);
    expect(hungWait.details).toMatchObject({ state: "error", cause: "timeout", cursor: "4" });
    expect(hungWait.isError).toBe(true);
    expect((await harness(() => killed).client.show("j", AbortSignal.abort(), dir)).details.state).toBe("cancelled");
  });
});

describe("needs", () => {
  const report = (cursor: string, tail = "This list is a hint; re-read the state with show before acting.") =>
    `needs: 0 need you, 0 gate, 0 blocked\ncursor: ${cursor}\n${tail}`;

  test("lists without or with a cursor and preserves values above 2^53", async () => {
    const big = "9007199254740993123456789";
    const { client, calls } = harness(() => ok(report(big)));
    const first = await client.needs({}, undefined, dir);
    const second = await client.needs({ cursor: "0012" }, undefined, dir);
    expect(calls.map(call => call.args)).toEqual([["--root", ROOT, "needs"], ["--root", ROOT, "needs", "--from", "0012"]]);
    expect(first.details).toMatchObject({ state: "ok", cursor: big });
    expect(second.details.cursor).toBe(big);
    expect(first.details.report).toBe(report(big));
    expect(text(first)).toContain("show before acting");
  });

  test("waits quietly with --wait --from --timeout, a deadline of timeout plus 5 seconds and no --include-done", async () => {
    const { client, calls } = harness(() => ok(report("18446744073709551616")));
    await client.needs({ cursor: "7", wait: true, timeout_seconds: 300 }, undefined, dir);
    await client.needs({ cursor: "7", wait: true }, undefined, dir);
    expect(calls[0]!.args).toEqual(["--root", ROOT, "needs", "--wait", "--from", "7", "--timeout", "300"]);
    expect(calls[0]!.options.timeout).toBe(305_000);
    expect(calls[1]!.args.slice(-2)).toEqual(["--timeout", "60"]);
    expect(calls[1]!.options.timeout).toBe(65_000);
    expect(calls.flatMap(call => call.args)).not.toContain("--include-done");
  });

  test("treats exit 3 as an ordinary timeout and keeps the supplied cursor", async () => {
    const { client } = harness(() => ({ stdout: "needs: nothing new", stderr: "", code: 3, killed: false }));
    const result = await client.needs({ cursor: "9007199254740993", wait: true, timeout_seconds: 1 }, undefined, dir);
    expect(result.details).toMatchObject({ state: "timeout", cursor: "9007199254740993" });
    expect(result.isError).toBeUndefined();
    expect(result.details.process?.code).toBe(3);
  });

  test("exit 3 without --wait, and exits 1 and 2, are errors", async () => {
    for (const [code, wait] of [[3, false], [1, true], [2, true]] as const) {
      const { client } = harness(() => ({ stdout: "", stderr: "taskwatch: bad", code, killed: false }));
      const result = await client.needs({ cursor: "1", wait }, undefined, dir);
      expect(result.details.state).toBe("error");
      expect(text(result)).toContain(`exit ${code}`);
    }
  });

  test.each([
    ["missing", "needs: 0 need you\nthis list is a hint"],
    ["duplicate", `${report("1")}\ncursor: 2`],
    ["invalid", report("12x")],
    ["signed", report("-1")],
    ["empty", "needs\ncursor: "],
  ])("a %s cursor token is an explicit error", async (_name, stdout) => {
    const { client } = harness(() => ok(stdout));
    const result = await client.needs({ cursor: "1" }, undefined, dir);
    expect(result.details).toMatchObject({ state: "error", report: stdout });
    expect(result.details.cursor).toBe("1");
    expect(() => cursorOf(stdout)).toThrow();
  });

  test.each([
    [{ wait: true }],
    [{ cursor: "1e3" }],
    [{ cursor: "-1" }],
    [{ cursor: "" }],
    [{ cursor: "1", wait: true, timeout_seconds: 0 }],
    [{ cursor: "1", wait: true, timeout_seconds: 301 }],
    [{ cursor: "1", wait: true, timeout_seconds: 1.5 }],
  ])("rejects %p before any process", async params => {
    const { client, calls } = harness(() => ok(report("1")));
    expect((await client.needs(params, undefined, dir)).details.state).toBe("error");
    expect(calls).toEqual([]);
  });

  test("never wakes anything: a clean completion report returns once and arms nothing", async () => {
    const { client, calls } = harness(() => ok(report("4", "FINISHED SINCE THE CURSOR\njob job-id-1 done")));
    await client.needs({ cursor: "3", wait: true }, undefined, dir);
    expect(calls).toHaveLength(1);
  });
});

describe("display bound", () => {
  test("caps all text at 8 KiB at a code point boundary and keeps full details", async () => {
    const tail = "가🙂é".repeat(5000);
    const cursor = "9".repeat(40);
    const stdout = `needs: 1\ncursor: ${cursor}\n${tail}`;
    const { client } = harness(() => ok(stdout));
    const result = await client.needs({}, undefined, dir);
    const shown = text(result);
    expect(Buffer.byteLength(shown)).toBeLessThanOrEqual(8192);
    expect(shown).not.toContain("\uFFFD");
    expect(shown).toContain("details");
    expect(result.details.report).toBe(stdout);
    expect(result.details.cursor).toBe(cursor);
  });

  test("bound cuts every multibyte alignment cleanly", () => {
    for (const prefix of ["", "a", "aa", "aaa"]) {
      for (const unit of ["é", "가", "🙂"]) {
        const shown = bound(prefix + unit.repeat(9000));
        expect(Buffer.byteLength(shown)).toBeLessThanOrEqual(8192);
        expect(shown).not.toContain("\uFFFD");
      }
    }
    expect(bound("short")).toBe("short");
  });

  test("never shortens identities or the authoritative record to fit the display", async () => {
    const id = "j".repeat(20_000);
    const receipt = "r".repeat(20_000);
    const full = record({ id, receipt_key: receipt, activity: "é".repeat(20_000) });
    const file = await jobFile("job.json", receipt);
    const { client } = harness(() => ok(JSON.stringify(full)));
    const result = await client.submit(file, undefined, dir);
    expect(Buffer.byteLength(text(result))).toBeLessThanOrEqual(8192);
    expect(result.details).toMatchObject({ job_id: id, receipt_key: receipt, record: full });
  });
});

describe("waits, abort and lifecycle", () => {
  test("propagates the caller signal and removes its listener", async () => {
    const seen: AbortSignal[] = [];
    const { client, reached } = harness((_args, options) => { seen.push(options.signal!); return untilAborted(options.signal); });
    const controller = new AbortController();
    const pending = client.needs({ cursor: "1", wait: true }, controller.signal, dir);
    await reached(1);
    expect(getEventListeners(controller.signal, "abort")).toHaveLength(1);
    controller.abort();
    expect((await pending).details.state).toBe("cancelled");
    expect(seen[0]!.aborted).toBe(true);
    expect(getEventListeners(controller.signal, "abort")).toHaveLength(0);
    const done = harness(() => ok("{}"));
    const ctl = new AbortController();
    await done.client.show("j", ctl.signal, dir);
    expect(getEventListeners(ctl.signal, "abort")).toHaveLength(0);
  });

  test("a replacement wait aborts and awaits the old wait before launching, and the old finally keeps the new owner", async () => {
    const log: string[] = [];
    let live = 0;
    let peak = 0;
    const { client, calls, reached } = harness(async (args, options) => {
      const name = `w${calls.length}`;
      log.push(`start ${name}`);
      peak = Math.max(peak, ++live);
      const result = await untilAborted(options.signal, 20);
      live--;
      log.push(`end ${name}`);
      return { ...result, stdout: args.join(" ") };
    });
    const first = client.needs({ cursor: "1", wait: true }, undefined, dir);
    await reached(1);
    const second = client.needs({ cursor: "2", wait: true }, undefined, dir);
    expect((await first).details).toMatchObject({ state: "cancelled", cursor: "1" });
    await reached(2);
    const third = client.needs({ cursor: "3", wait: true }, undefined, dir);
    expect((await second).details).toMatchObject({ state: "cancelled", cursor: "2" });
    await client.close();
    expect((await third).details.state).toBe("cancelled");
    expect(log).toEqual(["start w1", "end w1", "start w2", "end w2", "start w3", "end w3"]);
    expect(peak).toBe(1);
  });

  test("simultaneous waits leave at most one live process", async () => {
    let live = 0;
    let peak = 0;
    const { client, reached } = harness(async (_args, options) => {
      peak = Math.max(peak, ++live);
      const result = await untilAborted(options.signal, 10);
      live--;
      return result;
    });
    const first = client.needs({ cursor: "0", wait: true }, undefined, dir);
    await reached(1);
    const waits = [first, ...["1", "2", "3"].map(cursor => client.needs({ cursor, wait: true }, undefined, dir))];
    await client.close();
    const states = (await Promise.all(waits)).map(result => result.details.state);
    expect(states).toEqual(["cancelled", "cancelled", "cancelled", "cancelled"]);
    expect(peak).toBe(1);
  });

  test("an invalid replacement does not disturb the live wait", async () => {
    const { client, reached } = harness((_args, options) => untilAborted(options.signal));
    const live = client.needs({ cursor: "1", wait: true }, undefined, dir);
    await reached(1);
    expect((await client.needs({ wait: true }, undefined, dir)).details.state).toBe("error");
    await client.close();
    expect((await live).details.cause).toBe("session");
  });

  test("non-wait calls run beside a wait", async () => {
    const { client, reached } = harness((args, options) => (args.includes("--wait") ? untilAborted(options.signal) : ok(JSON.stringify(record()))));
    const live = client.needs({ cursor: "1", wait: true }, undefined, dir);
    await reached(1);
    expect((await client.show("job-id-1", undefined, dir)).details.state).toBe("ok");
    await client.close();
    await live;
  });

  test("close aborts and awaits every call kind and fences their results", async () => {
    const file = await jobFile("job.json");
    const finished: string[] = [];
    const { client, reached } = harness(async (args, options) => {
      // Even a call that completes successfully during close must not be returned.
      const result = args.includes("--wait") ? await untilAborted(options.signal, 15) : (await untilAborted(options.signal, 15), ok(JSON.stringify(record())));
      finished.push(args[2]!);
      return result;
    });
    const calls = [
      client.submit(file, undefined, dir),
      client.show("job-id-1", undefined, dir),
      client.needs({}, undefined, dir),
      client.needs({ cursor: "5", wait: true }, undefined, dir),
    ];
    await reached(4);
    await client.close();
    expect(finished.sort()).toEqual(["needs", "needs", "show", "submit"]);
    const [submit, show, plain, wait] = await Promise.all(calls);
    expect(submit!.details).toMatchObject({ state: "unknown", cause: "session", receipt_key: "approval:job-1" });
    expect(show!.details).toMatchObject({ state: "cancelled", cause: "session" });
    expect(plain!.details).toMatchObject({ state: "cancelled", cause: "session" });
    expect(wait!.details).toMatchObject({ state: "cancelled", cause: "session", cursor: "5" });
    for (const result of [submit!, show!, plain!, wait!]) {
      expect(result.details.record).toBeUndefined();
      expect(result.details.job_id).toBeUndefined();
    }
  });

  test("a fresh session after close inherits nothing and no timer or listener remains", async () => {
    const { client, calls, reached } = harness((args, options) => (args.includes("--wait") ? untilAborted(options.signal) : ok(JSON.stringify(record()))));
    const controller = new AbortController();
    const old = client.needs({ cursor: "1", wait: true }, controller.signal, dir);
    await reached(1);
    await client.close();
    await old;
    expect(getEventListeners(controller.signal, "abort")).toHaveLength(0);
    const fresh = await client.show("job-id-1", undefined, dir);
    expect(fresh.details.state).toBe("ok");
    expect(calls[1]!.options.signal!.aborted).toBe(false);
    await client.close();
  });

  test("a call arriving during close waits for the old session to finish disposing", async () => {
    const order: string[] = [];
    const { client, reached } = harness(async (args, options) => {
      if (args.includes("--wait")) {
        const result = await untilAborted(options.signal, 20);
        order.push("old disposed");
        return result;
      }
      order.push("new started");
      return ok(JSON.stringify(record()));
    });
    const old = client.needs({ cursor: "1", wait: true }, undefined, dir);
    await reached(1);
    const closing = client.close();
    const fresh = client.show("job-id-1", undefined, dir);
    await Promise.all([closing, old]);
    expect((await fresh).details.state).toBe("ok");
    expect(order).toEqual(["old disposed", "new started"]);
  });

  test("never invokes any taskwatch command except submit, show and needs", async () => {
    const file = await jobFile("job.json");
    const { client, calls, reached } = harness((args, options) => (args[2] === "needs" && args.includes("--wait") ? untilAborted(options.signal) : ok(JSON.stringify(record()))));
    await client.submit(file, undefined, dir);
    await client.show("job-id-1", undefined, dir);
    const wait = client.needs({ cursor: "1", wait: true }, undefined, dir);
    await reached(3);
    await client.close();
    await wait;
    expect(new Set(calls.map(call => call.args[2]))).toEqual(new Set(["submit", "show", "needs"]));
  });
});

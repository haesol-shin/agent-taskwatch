import { describe, expect, test } from "bun:test";
import { type ExtensionAPI, type ExtensionContext, zod } from "@oh-my-pi/pi-coding-agent";
import type { Details } from "./client";
import taskwatch from "./index";

type Tool = Parameters<ExtensionAPI["registerTool"]>[0];
type Exec = ExtensionAPI["exec"];
type Handler = () => unknown;

/**
 * Records what the factory registers. Only the members the extension may touch exist; any
 * other API call (messages, commands, providers, shortcuts) throws, which proves absence.
 */
function fixture(exec: Exec, root: string | undefined) {
  const tools = new Map<string, Tool>();
  const flags = new Map<string, { type: string }>();
  const handlers = new Map<string, Handler[]>();
  const execs: string[][] = [];
  const sent: string[] = [];
  const forbidden = (name: string) => () => {
    throw new Error(`unexpected ExtensionAPI.${name}`);
  };
  const api = {
    zod,
    registerTool: (tool: Tool) => void tools.set(tool.name, tool),
    registerFlag: (name: string, options: { type: string }) => void flags.set(name, options),
    getFlag: (name: string) => (name === "taskwatch-root" ? root : undefined),
    on: (event: string, handler: Handler) => void handlers.set(event, [...(handlers.get(event) ?? []), handler]),
    exec: (command: string, args: string[], options?: Parameters<Exec>[2]) => {
      execs.push([command, ...args]);
      return exec(command, args, options);
    },
    sendMessage: (...args: unknown[]) => void sent.push(`sendMessage ${args.length}`),
    sendUserMessage: (...args: unknown[]) => void sent.push(`sendUserMessage ${args.length}`),
    registerCommand: forbidden("registerCommand"),
    registerShortcut: forbidden("registerShortcut"),
    registerProvider: forbidden("registerProvider"),
    setModel: forbidden("setModel"),
    setThinkingLevel: forbidden("setThinkingLevel"),
  };
  // The fixture implements only part of the public interface; the cast is the test seam.
  taskwatch(api as unknown as ExtensionAPI);
  return { tools, flags, handlers, execs, sent };
}

const ctx = { cwd: "/tmp" } as unknown as ExtensionContext;
const call = async (tool: Tool | undefined, params: unknown, signal?: AbortSignal) =>
  (await (tool as Tool).execute("call-1", params as never, signal, undefined, ctx)) as { details: Details };
const idle: Exec = async () => ({ stdout: "needs: 0\ncursor: 12\nhint", stderr: "", code: 0, killed: false });

describe("registration", () => {
  test("registers exactly the three tools and one string flag without running anything", () => {
    const { tools, flags, handlers, execs, sent } = fixture(idle, "/srv/taskwatch-example");
    expect([...tools.keys()].sort()).toEqual(["taskwatch_needs", "taskwatch_show", "taskwatch_submit"]);
    expect([...flags.entries()]).toEqual([["taskwatch-root", expect.objectContaining({ type: "string" })]]);
    expect([...handlers.keys()].sort()).toEqual(["session_shutdown", "session_switch"]);
    expect(execs).toEqual([]);
    expect(sent).toEqual([]);
  });

  test("schemas apply the documented defaults and bounds", () => {
    const needs = fixture(idle, "/r").tools.get("taskwatch_needs")!.parameters as unknown as { parse(value: unknown): unknown };
    expect(needs.parse({})).toEqual({ wait: false, timeout_seconds: 60 });
    expect(needs.parse({ cursor: "9007199254740993", wait: true, timeout_seconds: 300 })).toEqual({ cursor: "9007199254740993", wait: true, timeout_seconds: 300 });
    for (const bad of [{ timeout_seconds: 0 }, { timeout_seconds: 301 }, { timeout_seconds: 1.5 }, { cursor: "1e3" }, { cursor: 12 }]) {
      expect(() => needs.parse(bad)).toThrow();
    }
    const submit = fixture(idle, "/r").tools.get("taskwatch_submit")!.parameters as unknown as { parse(value: unknown): unknown };
    expect(() => submit.parse({})).toThrow();
    expect(() => submit.parse({ job_file: "" })).toThrow();
  });
});

describe("registered tools", () => {
  test("reads the root flag at call time and never falls back", async () => {
    const missing = fixture(idle, undefined);
    const result = await call(missing.tools.get("taskwatch_show"), { job_id: "j" });
    expect(result.details).toMatchObject({ state: "error" });
    expect(missing.execs).toEqual([]);
    const set = fixture(idle, "/srv/taskwatch-example");
    await call(set.tools.get("taskwatch_needs"), { wait: false, timeout_seconds: 60 });
    expect(set.execs).toEqual([["taskwatch", "--root", "/srv/taskwatch-example", "needs"]]);
  });

  test("a clean completion returns once and sends no message or turn", async () => {
    const { tools, execs, sent } = fixture(idle, "/srv/taskwatch-example");
    const result = await call(tools.get("taskwatch_needs"), { cursor: "11", wait: true, timeout_seconds: 1 });
    expect(result.details).toMatchObject({ state: "ok", cursor: "12" });
    expect(execs).toHaveLength(1);
    expect(sent).toEqual([]);
  });

  test("session_switch and session_shutdown abort the wait and fence its result", async () => {
    for (const event of ["session_switch", "session_shutdown"]) {
      const { promise: started, resolve } = Promise.withResolvers<AbortSignal>();
      const exec: Exec = async (_command, _args, options) => {
        resolve(options!.signal!);
        const { promise, resolve: done } = Promise.withResolvers<void>();
        options!.signal!.addEventListener("abort", () => done(), { once: true });
        await promise;
        return { stdout: "needs: 1\ncursor: 99\nhint", stderr: "", code: 0, killed: false };
      };
      const { tools, handlers, sent } = fixture(exec, "/srv/taskwatch-example");
      const pending = call(tools.get("taskwatch_needs"), { cursor: "5", wait: true, timeout_seconds: 60 });
      const signal = await started;
      await Promise.all(handlers.get(event)!.map(handler => handler()));
      expect(signal.aborted).toBe(true);
      const result = await pending;
      expect(result.details).toMatchObject({ state: "cancelled", cause: "session", cursor: "5" });
      expect(result.details.report).toBeUndefined();
      expect(sent).toEqual([]);
    }
  });
});

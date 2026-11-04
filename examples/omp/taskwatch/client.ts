import { readFile } from "node:fs/promises";
import { isAbsolute } from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

/** The public `pi.exec` signature; the client never spawns anything else. */
export type Exec = ExtensionAPI["exec"];

/** Structural copy of the public exec result, kept in details as captured diagnostics. */
export interface ProcessResult {
  stdout: string;
  stderr: string;
  code: number;
  killed: boolean;
}

export type Kind = "submit" | "show" | "needs";
/** `unknown` only describes a submit that may have committed before it was interrupted. */
export type State = "ok" | "error" | "timeout" | "cancelled" | "unknown";
export type Cause = "timeout" | "cancelled" | "session" | "malformed" | "transport";

export interface Details {
  tool: Kind;
  state: State;
  cause?: Cause;
  job_id?: string;
  receipt_key?: string;
  cursor?: string;
  record?: Record<string, unknown>;
  report?: string;
  process?: ProcessResult;
  error?: string;
}

export interface ToolResult {
  content: { type: "text"; text: string }[];
  details: Details;
  isError?: boolean;
  useless?: boolean;
}

export interface NeedsParams {
  cursor?: string;
  wait?: boolean;
  timeout_seconds?: number;
}

export const DISPLAY_LIMIT_BYTES = 8192;
const QUICK_DEADLINE_MS = 60_000;
const WAIT_GRACE_MS = 5_000;
const NOTICE = "\n[Output truncated at 8 KiB; the complete data is in the result details.]";

/** Cap UTF-8 bytes, cutting before a partial code point and naming where the rest lives. */
export function bound(text: string): string {
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length <= DISPLAY_LIMIT_BYTES) return text;
  let end = DISPLAY_LIMIT_BYTES - Buffer.byteLength(NOTICE);
  while (end > 0 && (bytes[end]! & 0xc0) === 0x80) end--;
  return bytes.subarray(0, end).toString("utf8") + NOTICE;
}

function nonempty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/** One JSON object with nothing after it; stderr warnings are kept apart from stdout. */
function parseObject(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("expected one JSON object");
  }
  return value as Record<string, unknown>;
}

/** The only needs field the client reads: the documented `cursor: <digits>` transport line. */
export function cursorOf(report: string): string {
  const lines = report.split(/\r?\n/).filter(line => line.startsWith("cursor:"));
  const match = lines.length === 1 ? /^cursor: ([0-9]+)$/.exec(lines[0]!) : null;
  if (!match) throw new Error(`expected exactly one valid cursor line, found ${lines.length}`);
  return match[1]!;
}

interface Prepared {
  args: string[];
  deadlineMs: number;
  receipt?: string;
  cursor?: string;
  wait: boolean;
}

interface Call {
  controller: AbortController;
  disposed: Promise<void>;
}

function identity(prepared: Prepared | undefined): Pick<Details, "receipt_key" | "cursor"> {
  return { receipt_key: prepared?.receipt, cursor: prepared?.cursor };
}

/**
 * Runs `taskwatch --root <root> submit|show|needs` through the public exec and owns the
 * in-flight calls of one session. It never decides durable lifecycle: show JSON and the
 * submit receipt do.
 */
export class TaskwatchClient {
  #generation = 0;
  #active = new Set<Call>();
  #wait?: Call;
  #closing?: Promise<void>;

  constructor(
    private readonly exec: Exec,
    private readonly root: () => unknown,
  ) {}

  submit(jobFile: string, caller: AbortSignal | undefined, cwd: string): Promise<ToolResult> {
    return this.#run("submit", caller, cwd, async () => {
      const root = this.#root();
      if (!isAbsolute(jobFile)) throw new Error("job_file must be an absolute path");
      const job = parseObject(await readFile(jobFile, "utf8"));
      if (!nonempty(job.receipt_key)) throw new Error("job_file needs a nonempty receipt_key");
      return { args: ["--root", root, "submit", jobFile], deadlineMs: QUICK_DEADLINE_MS, receipt: job.receipt_key, wait: false };
    });
  }

  show(jobId: string, caller: AbortSignal | undefined, cwd: string): Promise<ToolResult> {
    return this.#run("show", caller, cwd, () => {
      const root = this.#root();
      if (!nonempty(jobId)) throw new Error("job_id must be nonempty");
      return { args: ["--root", root, "show", jobId], deadlineMs: QUICK_DEADLINE_MS, wait: false };
    });
  }

  needs(params: NeedsParams, caller: AbortSignal | undefined, cwd: string): Promise<ToolResult> {
    return this.#run("needs", caller, cwd, () => {
      const root = this.#root();
      const { cursor, wait = false, timeout_seconds: seconds = 60 } = params;
      if (cursor !== undefined && !/^[0-9]+$/.test(cursor)) throw new Error("cursor must be a decimal string");
      if (!Number.isInteger(seconds) || seconds < 1 || seconds > 300) throw new Error("timeout_seconds must be an integer from 1 to 300");
      if (wait && cursor === undefined) throw new Error("wait requires the cursor of an earlier needs report");
      const args = ["--root", root, "needs"];
      if (wait) args.push("--wait");
      if (cursor !== undefined) args.push("--from", cursor);
      if (wait) args.push("--timeout", String(seconds));
      return { args, deadlineMs: wait ? seconds * 1000 + WAIT_GRACE_MS : QUICK_DEADLINE_MS, cursor, wait };
    });
  }

  /** Abort and await every call of the session that ended; their results are fenced. */
  async close(): Promise<void> {
    this.#generation++;
    const calls = [...this.#active];
    for (const call of calls) call.controller.abort();
    const closing = Promise.all(calls.map(call => call.disposed)).then(() => undefined);
    this.#closing = closing;
    await closing;
    if (this.#closing === closing) this.#closing = undefined;
  }

  #root(): string {
    const root = this.root();
    if (!nonempty(root) || !isAbsolute(root)) {
      throw new Error("Set --taskwatch-root to a nonempty absolute taskwatch root before using taskwatch tools");
    }
    return root;
  }

  async #run(kind: Kind, caller: AbortSignal | undefined, cwd: string, prepare: () => Prepared | Promise<Prepared>): Promise<ToolResult> {
    while (this.#closing) await this.#closing;
    const epoch = this.#generation;
    const { promise: disposed, resolve: release } = Promise.withResolvers<void>();
    const call: Call = { controller: new AbortController(), disposed };
    const forward = () => call.controller.abort();
    this.#active.add(call);
    caller?.addEventListener("abort", forward, { once: true });
    if (caller?.aborted) forward();
    let prepared: Prepared | undefined;
    let launched = false;
    let process: ProcessResult | undefined;
    let result: ToolResult;
    try {
      prepared = await prepare();
      let previous: Call | undefined;
      if (prepared.wait) {
        previous = this.#wait;
        this.#wait = call;
      }
      previous?.controller.abort();
      await previous?.disposed;
      if (call.controller.signal.aborted) {
        result = this.#interrupted(kind, prepared, call, undefined);
      } else {
        launched = true;
        process = await this.exec("taskwatch", prepared.args, { cwd, signal: call.controller.signal, timeout: prepared.deadlineMs });
        result = process.killed || call.controller.signal.aborted
          ? this.#interrupted(kind, prepared, call, process)
          : this.#finish(kind, prepared, process);
      }
    } catch (error) {
      result = this.#failed(kind, prepared, launched, process, error);
    } finally {
      caller?.removeEventListener("abort", forward);
      this.#active.delete(call);
      if (this.#wait === call) this.#wait = undefined;
      release();
    }
    return epoch === this.#generation ? result : this.#fenced(kind, prepared);
  }

  #interrupted(kind: Kind, prepared: Prepared, call: Call, process: ProcessResult | undefined): ToolResult {
    const cause: Cause = call.controller.signal.aborted ? "cancelled" : "timeout";
    const base = { tool: kind, process, cause, ...identity(prepared) };
    if (kind === "submit" && process) return this.#submitUnknown(cause, base, process);
    if (cause === "timeout") {
      return this.#error(base, `taskwatch ${kind} was killed at the client deadline without a complete answer`);
    }
    const text = `taskwatch ${kind} was cancelled; `
      + (process ? "only the client process ended; the durable job is unchanged." : "no taskwatch process was started.");
    return { content: [{ type: "text", text }], details: { ...base, state: "cancelled" }, useless: true };
  }

  #submitUnknown(cause: Cause, base: Partial<Details>, process?: ProcessResult): ToolResult {
    const text = `Submission outcome unknown (${cause}). The job may or may not exist; it was not cancelled. `
      + `Replay the same existing file with receipt_key ${base.receipt_key} to reconcile.`;
    return {
      content: [{ type: "text", text: bound(text) }],
      details: { ...base, tool: "submit", state: "unknown", cause, process },
      isError: true,
    };
  }

  #fenced(kind: Kind, prepared: Prepared | undefined): ToolResult {
    const base = { tool: kind, cause: "session" as const, ...identity(prepared) };
    if (kind === "submit" && prepared?.receipt) return this.#submitUnknown("session", base);
    return {
      content: [{ type: "text", text: "The session ended; this client call was cancelled and its result discarded." }],
      details: { ...base, state: "cancelled" },
      useless: true,
    };
  }

  #failed(kind: Kind, prepared: Prepared | undefined, launched: boolean, process: ProcessResult | undefined, error: unknown): ToolResult {
    const message = error instanceof Error ? error.message : String(error);
    const base = { tool: kind, process, ...identity(prepared) };
    if (/not found|ENOENT/i.test(message) && launched) {
      return this.#error(base, `taskwatch executable was not found on PATH: ${message}`);
    }
    if (kind === "submit" && launched) return this.#submitUnknown("transport", { ...base, error: message }, process);
    return this.#error(base, message);
  }

  #error(base: Partial<Details> & { tool: Kind }, message: string): ToolResult {
    return {
      content: [{ type: "text", text: bound(`taskwatch ${base.tool} failed: ${message}`) }],
      details: { ...base, state: "error", error: message },
      isError: true,
    };
  }

  #finish(kind: Kind, prepared: Prepared, process: ProcessResult): ToolResult {
    const base = { tool: kind, process, ...identity(prepared) };
    if (kind === "needs" && prepared.wait && process.code === 3) {
      return {
        content: [{ type: "text", text: `No new item needs attention before the wait deadline. Keep cursor ${prepared.cursor} and re-arm explicitly if needed.` }],
        details: { ...base, state: "timeout", cause: "timeout" },
        useless: true,
      };
    }
    if (process.code !== 0) {
      return this.#error(base, `exit ${process.code}: ${process.stderr.trim() || process.stdout.trim() || "no output"}`);
    }
    try {
      if (kind === "needs") {
        const cursor = cursorOf(process.stdout);
        return { content: [{ type: "text", text: bound(process.stdout) }], details: { ...base, state: "ok", cursor, report: process.stdout } };
      }
      return this.#record(kind, prepared, process);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (kind === "submit") return this.#submitUnknown("malformed", { ...base, error: message }, process);
      return this.#error({ ...base, report: kind === "needs" ? process.stdout : undefined }, `malformed reply: ${message}`);
    }
  }

  #record(kind: "submit" | "show", prepared: Prepared, process: ProcessResult): ToolResult {
    const record = parseObject(process.stdout);
    const id = record.id;
    if (!nonempty(id)) throw new Error("record has no nonempty id");
    if (kind === "show" && id !== prepared.args[3]) throw new Error(`record id ${id} differs from the requested job`);
    const base = { tool: kind, process, record, job_id: id, receipt_key: nonempty(record.receipt_key) ? record.receipt_key : undefined };
    if (kind === "submit" && record.receipt_key !== prepared.receipt) {
      return this.#error({ ...base, receipt_key: prepared.receipt }, `replay returned receipt_key ${String(record.receipt_key)}, not ${prepared.receipt}`);
    }
    const lines = [`id: ${id}`, `receipt_key: ${String(record.receipt_key)}`, `stage: ${String(record.stage)}`];
    if (record.activity !== undefined) lines.push(`activity: ${String(record.activity)}`);
    return { content: [{ type: "text", text: bound(lines.join("\n")) }], details: { ...base, state: "ok" } };
  }
}

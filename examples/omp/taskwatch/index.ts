import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { TaskwatchClient } from "./client";

/**
 * Optional native client over the installed `taskwatch` CLI. Registration happens at load;
 * subprocess work happens only inside tool calls. See docs/omp.md for the contract.
 */
export default function taskwatch(pi: ExtensionAPI): void {
  pi.registerFlag("taskwatch-root", {
    description: "Absolute taskwatch root used by the taskwatch_* tools (required)",
    type: "string",
  });
  const client = new TaskwatchClient(
    (command, args, options) => pi.exec(command, args, options),
    () => pi.getFlag("taskwatch-root"),
  );
  pi.on("session_switch", () => client.close());
  pi.on("session_shutdown", () => client.close());

  const { z } = pi.zod;
  const submit = z.object({ job_file: z.string().min(1).describe("Existing absolute path of the approved JSON file") });
  const show = z.object({ job_id: z.string().min(1).describe("Complete job id") });
  const needs = z.object({
    cursor: z.string().regex(/^[0-9]+$/).optional().describe("Opaque decimal cursor from an earlier report"),
    wait: z.boolean().default(false),
    timeout_seconds: z.number().int().min(1).max(300).default(60),
  });

  pi.registerTool({
    name: "taskwatch_submit",
    label: "Taskwatch submit",
    description:
      "Submit an existing approved job JSON file with taskwatch; never edits it. If the outcome is unknown, call again with the same file to replay its receipt_key.",
    parameters: submit,
    execute: (_id, params, signal, _update, ctx) => client.submit(params.job_file, signal, ctx.cwd),
  });
  pi.registerTool({
    name: "taskwatch_show",
    label: "Taskwatch show",
    description: "Read one job's authoritative JSON record. Use it before recommending any action.",
    parameters: show,
    execute: (_id, params, signal, _update, ctx) => client.show(params.job_id, signal, ctx.cwd),
  });
  pi.registerTool({
    name: "taskwatch_needs",
    label: "Taskwatch needs",
    description:
      "List what needs the owner, or with wait=true block once, quietly, from an earlier cursor until something needs attention or timeout_seconds passes. The report is a hint; use taskwatch_show before acting.",
    parameters: needs,
    execute: (_id, params, signal, _update, ctx) => client.needs(params, signal, ctx.cwd),
  });
}

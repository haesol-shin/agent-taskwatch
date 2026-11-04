/**
 * Child of smoke.ts, started with a PATH that has no `taskwatch`: Bun resolves executables from
 * the environment it was started with, so this case needs its own process. It loads the real
 * extension, runs the real `pi.exec` and prints the tool result as JSON.
 */
import { loadExtensions } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const [index, root, cwd] = process.argv.slice(2);
if (!index || !root || !cwd) throw new Error("usage: smoke-missing-cli.ts <index.ts> <root> <cwd>");
const loaded = await loadExtensions([index], cwd);
if (loaded.errors.length > 0) throw new Error(`load errors: ${JSON.stringify(loaded.errors)}`);
loaded.runtime.flagValues.set("taskwatch-root", root);
const tool = loaded.extensions[0]?.tools.get("taskwatch_show")?.definition;
if (!tool) throw new Error("taskwatch_show is not registered");
const result = await tool.execute("missing-cli", { job_id: "x" } as never, undefined, undefined, { cwd } as unknown as ExtensionContext);
console.log(JSON.stringify({ details: result.details, isError: result.isError, text: result.content }));

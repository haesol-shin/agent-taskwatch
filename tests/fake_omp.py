"""A fake `omp --mode rpc` for tests. Synthetic: no model, no network.

FAKE_OMP_SCENARIO is a JSON file: {"chunk_at": int|null, "turns": [turn, ...]}.
FAKE_OMP_STATE is a directory that holds the shared turn counter and log.jsonl, which
records every command received so a test can assert what the runner sent.

A turn runs when a prompt arrives (the Nth prompt over all processes takes turn N):
  writes: {path: text}      files written relative to the working directory
  run: [argv, ...]          commands run in the working directory
  pending_async: true       the session never settles and reports pending async work
  followup_error: stopReason of an async follow-up run that ends after prompt_result
  reply: str                the final assistant text
  die: true                 exit after agent_start, before the session has any content
  die_after_content: true   write the prompt to the session file, then exit
  no_agent_start, no_settle: skip that frame / never settle
  status: prompt_result status (default completed)
  sleep: seconds to wait before finishing the turn
  notice: emit a session-persistence error notice; oversize: emit a frame over the limit
  error: prompt_result error object, for example a quota failure with status "failed"
  (scenario) exit_code: exit code after stdin closes

Models. Each process launches on one model: `--model provider/id` on its command line, or
`--model @role` resolved now through roles.json in FAKE_OMP_STATE (a test rewrites that file
to change what the role means), else scenario `default_model` ("synthetic/default"). Every
session line records the model that served it, and a resumed session restores its saved model
unless open_session carries `provider` and `modelId` together. Scenario switches:
  startup_state: "missing" | "malformed"   get_state reports no usable model
  open: "ignore" | "reject" | "cancel" | "hang"
        ignore the model override, answer model-not-found, answer cancelled, never answer
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SCENARIO = json.loads(Path(os.environ["FAKE_OMP_SCENARIO"]).read_text(encoding="utf-8"))
STATE = Path(os.environ["FAKE_OMP_STATE"])
STATE.mkdir(parents=True, exist_ok=True)
FRAME_LIMIT = 1 << 20

protocol = 1
model = ""
chunks = 0
session_file: Path | None = None
settled = True
pending = False
reply = ""
LOCK = threading.RLock()  # one frame, or one chunk sequence, at a time
turns: list[threading.Thread] = []


def emit(frame: dict) -> None:
    with LOCK:
        _emit(frame)


def _emit(frame: dict) -> None:
    global chunks
    line = json.dumps(frame)
    limit = SCENARIO.get("chunk_at")
    if protocol == 2 and limit and len(line) + 1 > limit:
        data = line.encode("utf-8")
        size = max(1, limit // 2)
        parts = [data[i : i + size] for i in range(0, len(data), size)]
        chunks += 1
        for index, part in enumerate(parts):
            sys.stdout.write(
                json.dumps(
                    {
                        "type": "rpc_chunk",
                        "chunkId": f"rpc-{chunks}",
                        "index": index,
                        "count": len(parts),
                        "byteLength": len(data),
                        "data": base64.b64encode(part).decode("ascii"),
                    }
                )
                + "\n"
            )
    else:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def log(entry: dict) -> None:
    with (STATE / "log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def next_turn() -> dict:
    counter = STATE / "turn.txt"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    turns = SCENARIO.get("turns", [])
    if n >= len(turns):
        raise RuntimeError("fake RPC scenario has no remaining turn")
    return turns[n]


def respond(cmd: dict, data=None, ok: bool = True, error: str = "") -> None:
    frame = {"id": cmd.get("id"), "type": "response", "command": cmd["type"], "success": ok}
    if ok and data is not None:
        frame["data"] = data
    if not ok:
        frame["error"] = error
    emit(frame)


def launch_model() -> str:
    argv = sys.argv[1:]
    spec = SCENARIO.get("default_model", "synthetic/default")
    if "--model" in argv:
        spec = argv[argv.index("--model") + 1]
        if spec.startswith("@"):
            spec = json.loads((STATE / "roles.json").read_text(encoding="utf-8"))[spec]
    return spec


def saved_model(path: Path) -> str:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        entry = json.loads(line)
        if "model_change" in entry or "model" in entry:
            return entry.get("model_change") or entry["model"]
    return ""


def open_session(cmd: dict) -> None:
    global session_file, model
    mode = SCENARIO.get("open", "")
    if mode == "hang":
        time.sleep(30)
    if mode == "reject":
        respond(cmd, ok=False, error="Model not found: unknown/model")
        return
    if mode == "cancel":
        respond(cmd, {"cancelled": True})
        return
    directory = Path(cmd["sessionDir"])
    directory.mkdir(parents=True, exist_ok=True)
    # Like the public contract: only the newest nonempty session is resumable.
    existing = [f for f in sorted(directory.glob("*.jsonl")) if f.stat().st_size > 0]
    resumed = bool(existing)
    session_file = existing[-1] if existing else directory / "s1.jsonl"
    session_file.touch()
    override = None
    if mode != "ignore" and cmd.get("provider") and cmd.get("modelId"):
        override = f"{cmd['provider']}/{cmd['modelId']}"
    if resumed:
        saved = saved_model(session_file) or model
        model = override or saved
        if model != saved:
            with session_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"model_change": model}) + "\n")
    else:
        model = override or model
    respond(
        cmd,
        {
            "cancelled": False,
            "resumed": resumed,
            "sessionId": "sid",
            "sessionFile": str(session_file),
        },
    )


def model_state() -> dict:
    mode = SCENARIO.get("startup_state")
    if mode == "missing":
        return {}
    provider, _, ident = model.partition("/")
    if mode == "malformed":
        return {"model": {"provider": "", "id": 7}}
    return {"model": {"provider": provider, "id": ident}}


def record_prompt(message: str) -> None:
    if session_file is not None:
        with session_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"prompt": message, "model": model}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def prompt(cmd: dict) -> None:
    global settled, reply, pending
    turn = next_turn()
    settled = False
    respond(cmd, {})
    if not turn.get("no_agent_start"):
        emit({"type": "agent_start"})
    if turn.get("die") or turn.get("die_after_content"):
        if turn.get("die_after_content"):
            record_prompt(cmd["message"])
        os._exit(3)
    time.sleep(turn.get("sleep", 0))
    for rel, text in turn.get("writes", {}).items():
        if turn.get("require_write_grants") and not Path(rel).resolve().is_relative_to(Path.cwd()):
            grants = re.findall(r"^- `([^`]+)`$", cmd["message"], re.MULTILINE)
            if Path(rel).resolve() not in {Path(p).resolve() for p in grants}:
                raise PermissionError(f"no named write grant: {rel}")
        path = Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for argv in turn.get("run", []):
        subprocess.run(argv, check=True, capture_output=True)
    pending = bool(turn.get("pending_async"))
    reply = turn.get("reply", "")
    if turn.get("notice"):
        emit({"type": "notice", "level": "error", "source": "session-persistence", "message": "x"})
    if turn.get("oversize"):
        big = {"type": "message_update", "pad": "x" * (FRAME_LIMIT + 10)}
        with LOCK:
            sys.stdout.write(json.dumps(big) + "\n")
            sys.stdout.flush()
    record_prompt(cmd["message"])
    emit({"type": "agent_end", "messages": [], "isTerminal": True, "yielded": True})
    followup = turn.get("followup_error")
    emit(
        {
            "type": "prompt_result",
            "id": cmd["id"],
            "agentInvoked": True,
            "status": turn.get("status", "completed"),
            **({"error": turn["error"]} if "error" in turn else {}),
            "sessionSettled": not (turn.get("no_settle") or followup),
        }
    )
    if followup:
        # An async follow-up run fails after the one prompt_result; no second one is sent.
        emit({"type": "agent_start"})
        message = {"role": "assistant", "stopReason": followup, "content": []}
        emit({"type": "message_end", "message": message})
        emit({"type": "agent_end", "messages": [message], "isTerminal": True, "yielded": True})
    if not (turn.get("no_settle") or pending):
        settled = True
        emit({"type": "session_settled"})


def handle(cmd: dict) -> None:
    global protocol
    log(cmd)
    kind = cmd["type"]
    if kind == "negotiate_protocol":
        protocol = cmd["protocolVersion"]
        respond(cmd, {"protocolVersion": protocol})
    elif kind == "open_session":
        open_session(cmd)
    elif kind == "get_state":
        respond(cmd, {"isSettled": settled, "hasPendingAsyncWork": pending, **model_state()})
    elif kind == "prompt":
        # A real session keeps serving commands while a turn runs.
        thread = threading.Thread(target=prompt, args=(cmd,))
        turns.append(thread)
        thread.start()
    elif kind == "get_last_assistant_text":
        respond(cmd, {"text": reply})
    elif kind == "abort":
        pass
    else:
        respond(cmd, ok=False, error=f"unknown command {kind}")


def main() -> None:
    global model
    model = launch_model()
    log({"argv": sys.argv[1:], "cwd": os.getcwd()})
    emit(
        {
            "type": "ready",
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": FRAME_LIMIT,
            "maxReassembledFrameBytes": 64 << 20,
        }
    )
    for raw in sys.stdin:
        if raw.strip():
            handle(json.loads(raw))
    for thread in turns:
        thread.join()
    sys.exit(SCENARIO.get("exit_code", 0))


if __name__ == "__main__":
    main()

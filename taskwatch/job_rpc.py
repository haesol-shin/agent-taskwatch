"""Public omp RPC client over stdio (protocol v2): one process, JSONL frames.

Only documented frames are used. A prompt is not complete at its response or at
prompt_result: the caller also needs agent_start and a settled session. The client never
picks a model by policy: the process, as launched, reports the model omp selected, and
`open_session` binds the conversation to exactly that pair, so omp alone resolves roles.
"""

import base64
import binascii
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .rate_limit import RateLimit, prompt_rate_limit, scrub

DEFAULT_FRAME_BYTES = 1 << 20
DEFAULT_REASSEMBLED_BYTES = 64 << 20
OPEN_SESSION_NEEDS = (
    "the omp command must support the public open_session provider and modelId "
    'override (docs/configuration.md, "Model selection")'
)


class RpcError(Exception):
    """The RPC conversation failed. The step did not complete.

    `rate_limit` is set only for a terminal model HTTP 429 of a finished prompt."""

    def __init__(self, message: str = "", rate_limit: RateLimit | None = None) -> None:
        super().__init__(message)
        self.rate_limit = rate_limit


class RpcRejected(RpcError):
    """The peer answered a command with success: false."""


class RpcTimeout(RpcError):
    pass


class RpcAborted(RpcError):
    """The caller raised the abort flag, for example because its attempt went stale."""


class FrameDecoder:
    """Validate and reassemble rpc_chunk sequences; every other frame passes through."""

    def __init__(self, max_bytes: int = DEFAULT_REASSEMBLED_BYTES) -> None:
        self.max_bytes = max_bytes
        self._pending: dict | None = None

    def push(self, value: object) -> dict | None:
        if not isinstance(value, dict):
            raise RpcError("rpc frame must be an object")
        if value.get("type") != "rpc_chunk":
            if self._pending:
                raise RpcError("rpc chunk sequence interrupted")
            return value
        chunk_id, index = value.get("chunkId"), value.get("index")
        count, length = value.get("count"), value.get("byteLength")
        if not (isinstance(chunk_id, str) and 0 < len(chunk_id) <= 128) or any(
            type(item) is not int for item in (index, count, length)
        ):
            raise RpcError("invalid rpc chunk metadata")
        if count < 2 or not 0 <= index < count or not 0 < length <= self.max_bytes:
            raise RpcError("invalid rpc chunk metadata")
        data = _base64(value.get("data"))
        if self._pending is None:
            if index != 0:
                raise RpcError("rpc chunk sequence must start at index 0")
            self._pending = {
                "id": chunk_id,
                "count": count,
                "length": length,
                "next": 0,
                "parts": [],
            }
        pending = self._pending
        if (pending["id"], pending["count"], pending["length"], pending["next"]) != (
            chunk_id,
            count,
            length,
            index,
        ):
            raise RpcError("rpc chunk sequence mismatch")
        pending["parts"].append(data)
        pending["next"] += 1
        received = sum(len(part) for part in pending["parts"])
        if received > length:
            raise RpcError("rpc chunk sequence exceeds declared length")
        if pending["next"] < count:
            return None
        self._pending = None
        if received != length:
            raise RpcError("rpc chunk sequence length mismatch")
        try:
            frame = json.loads(b"".join(pending["parts"]).decode("utf-8"))
        except ValueError as exc:
            raise RpcError(f"rpc chunked frame is not valid JSON: {exc}") from exc
        if not isinstance(frame, dict):
            raise RpcError("rpc frame must be an object")
        return frame

    def finish(self) -> None:
        """End of stream: a sequence still open was cut off."""
        if self._pending:
            raise RpcError("rpc chunk sequence interrupted by end of stream")


def _base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise RpcError("invalid rpc chunk data")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RpcError("invalid rpc chunk data") from exc
    if base64.b64encode(data).decode("ascii") != value:
        raise RpcError("invalid rpc chunk data")
    return data


class RpcClient:
    def __init__(
        self,
        argv: Sequence[str],
        cwd: Path,
        *,
        on_frame: Callable[[dict], None] | None = None,
        stderr_path: Path | None = None,
        abort: threading.Event | None = None,
        poll: float = 2.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.on_frame = on_frame
        self.stderr_path = stderr_path
        self.abort = abort or threading.Event()
        self.poll = poll
        self.env = env
        self.max_frame = DEFAULT_FRAME_BYTES
        self._cv = threading.Condition()
        self._send_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr = None
        self._ready: dict | None = None
        self._responses: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self._agent_starts = 0
        self._settled = 0
        self._closed = False
        self._failure: RpcError | None = None
        self._counter = 0
        self._decoder = FrameDecoder()
        self._exit: int | None = None
        self._last_stop: object = None
        self._yield_stop: object = None

    def __enter__(self) -> "RpcClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close(kill=exc[0] is not None)

    def start(self, timeout: float) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        if self.stderr_path is not None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr = self.stderr_path.open("ab")
        try:
            self._proc = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr if self._stderr else subprocess.DEVNULL,
                env=dict(self.env) if self.env is not None else os.environ.copy(),
            )
        except OSError as exc:
            self._close_stderr()
            raise RpcError(f"cannot start omp: {exc}") from exc
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        ready = self._wait(lambda: self._ready, timeout, "ready frame")
        frame_limit = ready.get("maxFrameBytes")
        if type(frame_limit) is int and frame_limit > 0:
            self.max_frame = frame_limit
        versions = ready.get("supportedProtocolVersions")
        if not isinstance(versions, list) or 2 not in versions:
            raise RpcError("omp RPC protocol v2 is required for lossless review output")
        self.request("negotiate_protocol", timeout, protocolVersion=2)
        self._decoder.max_bytes = int(
            ready.get("maxReassembledFrameBytes") or DEFAULT_REASSEMBLED_BYTES
        )

    def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        decoder = self._decoder
        try:
            while raw := self._proc.stdout.readline(self.max_frame + 2):
                if not raw.endswith(b"\n"):
                    raise RpcError("rpc frame exceeds the frame limit or is truncated")
                if not raw.strip():
                    continue
                frame = decoder.push(json.loads(raw))
                if frame is not None:
                    self._dispatch(frame)
            decoder.finish()
        except (ValueError, RpcError) as exc:
            with self._cv:
                bad = exc if isinstance(exc, RpcError) else RpcError(f"bad rpc line: {exc}")
                self._failure = bad
        finally:
            with self._cv:
                self._closed = True
                self._cv.notify_all()

    def _dispatch(self, frame: dict) -> None:
        kind = frame.get("type")
        with self._cv:
            if kind == "ready":
                self._ready = frame
            elif kind == "response" and isinstance(frame.get("id"), str):
                self._responses[frame["id"]] = frame
            elif kind == "prompt_result" and isinstance(frame.get("id"), str):
                self._results[frame["id"]] = frame
            elif kind == "agent_start":
                self._agent_starts += 1
            elif kind == "message_end":
                self._note_message(frame.get("message"))
            elif kind == "agent_end":
                self._note_agent_end(frame)
            elif kind == "session_settled":
                self._settled += 1
            elif _is_failure_frame(frame):
                text = json.dumps(frame)[:500]
                self._failure = self._failure or RpcError(f"omp reported: {text}")
            self._cv.notify_all()
        if self.on_frame is not None:
            try:
                self.on_frame(frame)
            except Exception as exc:  # noqa: BLE001 - the recorder must not kill the reader
                with self._cv:
                    self._failure = self._failure or RpcError(f"frame handler failed: {exc}")
                    self._cv.notify_all()

    def _note_message(self, message: object) -> None:
        if isinstance(message, dict) and message.get("role") == "assistant":
            self._last_stop = message.get("stopReason")

    def _note_agent_end(self, frame: dict) -> None:
        for message in frame.get("messages") or ():
            self._note_message(message)
        # A later yielded end (an async follow-up) replaces the earlier outcome and sends no
        # prompt_result of its own; rpc-prompt-results.ts reports only the first yield.
        if frame.get("yielded", frame.get("isTerminal") is not False):
            self._yield_stop = self._last_stop

    def _wait(self, predicate: Callable[[], object], timeout: float, what: str):
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self.abort.is_set():
                    raise RpcAborted("aborted by the caller")
                if self._failure is not None:
                    raise self._failure
                value = predicate()
                if value:
                    return value
                if self._closed:
                    code = self._proc.poll() if self._proc else None
                    raise RpcError(f"omp exited (code {code}) while waiting for {what}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RpcTimeout(f"timed out waiting for {what}")
                self._cv.wait(min(remaining, 0.2))

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > self.max_frame:
            raise RpcError("command exceeds the RPC frame limit")
        assert self._proc is not None and self._proc.stdin is not None
        with self._send_lock:
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise RpcError(f"cannot write to omp: {exc}") from exc

    def _next_id(self) -> str:
        with self._cv:
            self._counter += 1
            return f"tw-{self._counter}"

    def request(self, kind: str, timeout: float, **fields) -> dict:
        ident = self._next_id()
        self._send({"id": ident, "type": kind, **fields})
        frame = self._wait(lambda: self._responses.pop(ident, None), timeout, kind)
        if not frame.get("success"):
            raise RpcRejected(f"{kind} failed: {frame.get('error', 'unknown error')}")
        data = frame.get("data")
        return data if isinstance(data, dict) else {}

    def startup_model(self, timeout: float) -> tuple[str, str]:
        """The `(provider, id)` omp selected when this process launched, from `get_state`."""
        return _model_pair(self.request("get_state", timeout))

    def open_session(
        self, session_dir: Path, timeout: float, *, provider: str, model_id: str
    ) -> dict:
        """Bind the process to the conversation in `session_dir` and to the given model.

        The pair replaces the model saved in the conversation. A peer that ignores it is
        caught by reading the model back, so a stale selection never answers a prompt."""
        try:
            data = self.request(
                "open_session",
                timeout,
                sessionDir=str(session_dir),
                provider=provider,
                modelId=model_id,
            )
        except RpcRejected as exc:
            raise RpcError(f"{exc}; {OPEN_SESSION_NEEDS}") from exc
        if data.get("cancelled"):
            raise RpcError("open_session was cancelled")
        bound = self.startup_model(timeout)
        if bound != (provider, model_id):
            raise RpcError(
                f"open_session bound {bound[0]}/{bound[1]}, not {provider}/{model_id}; "
                + OPEN_SESSION_NEEDS
            )
        return data

    def prompt_and_wait(self, message: str, timeout: float) -> dict:
        """Send one prompt; return prompt_result once started, completed and settled."""
        deadline = time.monotonic() + timeout
        with self._cv:
            starts, settled = self._agent_starts, self._settled
            self._yield_stop = None
        ident = self._next_id()
        self._send({"id": ident, "type": "prompt", "message": message})
        try:
            ack = self._wait(
                lambda: self._responses.pop(ident, None), min(timeout, 120), "prompt ack"
            )
            if not ack.get("success"):
                raise RpcError(f"prompt failed: {ack.get('error', 'unknown error')}")
            data = ack.get("data")
            if isinstance(data, dict) and data.get("agentInvoked") is False:
                raise RpcError("the prompt finished without an agent turn")
            result = self._wait(lambda: self._results.pop(ident, None), _left(deadline), "result")
            _accept_result(result)
            with self._cv:
                if self._agent_starts <= starts:
                    raise RpcError("no agent_start was seen for the prompt")
            self._wait_settled(deadline, settled)
            with self._cv:
                stop = self._yield_stop
            if stop in {"error", "aborted"}:
                raise RpcError(f"the final run of the session ended with stopReason {stop}")
        except RpcTimeout:
            self._abort_quietly()
            raise
        return result

    def _wait_settled(self, deadline: float, seen: int) -> None:
        while True:
            left = _left(deadline)
            state = self.request("get_state", left)
            if state.get("isSettled") is True and not state.get("hasPendingAsyncWork"):
                return
            with self._cv:
                if self._settled > seen and state.get("isSettled") is not False:
                    return
            try:
                self._wait(lambda: self._settled > seen, min(self.poll, _left(deadline)), "settle")
            except RpcTimeout:
                if time.monotonic() >= deadline:
                    raise

    def _abort_quietly(self) -> None:
        try:
            self._send({"type": "abort"})
        except RpcError:
            pass

    def last_assistant_text(self, timeout: float) -> str:
        text = self.request("get_last_assistant_text", timeout).get("text")
        return text if isinstance(text, str) else ""

    def close(self, kill: bool = False) -> int | None:
        proc = self._proc
        code = self._exit
        if proc is not None and self._exit is None:
            if kill and proc.poll() is None:
                proc.kill()
            else:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except OSError:
                    pass
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            if self._reader:
                self._reader.join(timeout=5)
            if proc.stdout:
                proc.stdout.close()
        self._close_stderr()
        self._exit = code
        return code

    def finish(self) -> None:
        """Close after a settled step. A failure notice or a nonzero exit fails the step."""
        code = self.close()
        if self._failure is not None:
            raise self._failure
        if code != 0:
            raise RpcError(f"omp exited with code {code}")

    def _close_stderr(self) -> None:
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None


def _is_failure_frame(frame: dict) -> bool:
    if frame.get("type") == "rpc_frame_error":
        return True
    return (
        frame.get("type") == "notice"
        and frame.get("level") == "error"
        and frame.get("source") == "session-persistence"
    )


def _model_pair(state: dict) -> tuple[str, str]:
    model = state.get("model")
    provider = model.get("provider") if isinstance(model, dict) else None
    ident = model.get("id") if isinstance(model, dict) else None
    if not (isinstance(provider, str) and provider and isinstance(ident, str) and ident):
        raise RpcError("get_state reported no model provider and id for the launched omp")
    return provider, ident


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise RpcTimeout("timed out")
    return left


def _accept_result(result: dict) -> None:
    if result.get("agentInvoked") is not True:
        raise RpcError("prompt_result says no agent ran")
    status = result.get("status")
    if status != "completed":
        error = result.get("error")
        detail = error.get("message") if isinstance(error, dict) else ""
        # Only the terminal error of this prompt is read, never conversation or tool text.
        limit = None
        if status in {"error", "failed"} and isinstance(detail, str):
            limit = prompt_rate_limit(detail)
        if limit is not None:
            raise RpcError(scrub(f"prompt ended {status}: {detail}"), limit)
        raise RpcError(f"prompt ended {status}: {detail}")

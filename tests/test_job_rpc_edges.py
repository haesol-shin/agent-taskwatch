import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from taskwatch.job_rpc import RpcClient, RpcError, _accept_result

PEER = r"""
import base64, json, sys
out = lambda f: print(json.dumps(f), flush=True)
out({"type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2]})
for line in sys.stdin:
    cmd = json.loads(line)
    out({"type": "response", "id": cmd["id"], "command": cmd["type"], "success": True, "data": {}})
part = base64.b64encode(b"{").decode()
out({"type": "rpc_chunk", "chunkId": "cut", "index": 0, "count": 2, "byteLength": 40, "data": part})
"""


def test_chunk_sequence_cut_off_by_peer_shutdown_fails_the_step(tmp_path):
    with RpcClient([sys.executable, "-c", PEER], tmp_path) as rpc:
        rpc.start(5)
        with pytest.raises(RpcError, match="interrupted"):
            rpc.finish()


def test_startup_error_uses_only_current_stderr_and_reaps_peer(tmp_path):
    stderr = tmp_path / "stderr"
    stderr.write_text("stale 429 retry-after-ms=90000\n")
    peer = (
        "import sys; "
        "print('Unknown tool: ast_grep. Valid tools: read, task', file=sys.stderr); "
        "print('Authorization: Bearer synthetic-secret', file=sys.stderr); sys.exit(7)"
    )
    rpc = RpcClient([sys.executable, "-c", peer], tmp_path, stderr_path=stderr)
    with pytest.raises(RpcError) as info:
        rpc.start(5)
    text = str(info.value)
    assert "ast_grep" in text and "read, task" in text and "7" in text
    assert "stale" not in text and "synthetic-secret" not in text
    assert info.value.rate_limit is None
    assert rpc.close() == 7


@pytest.mark.parametrize("kind", ["malformed", "timeout", "exit"])
def test_startup_failures_keep_cause_and_bound_diagnostic(tmp_path, kind):
    action = {
        "malformed": "print('not json', flush=True); time.sleep(60)",
        "timeout": "time.sleep(60)",
        "exit": "sys.exit(9)",
    }[kind]
    peer = (
        "import sys,time; sys.stderr.write('x' * 1000000 + '\\ncurrent failure\\n'); "
        f"sys.stderr.flush(); {action}"
    )
    rpc = RpcClient([sys.executable, "-c", peer], tmp_path, stderr_path=tmp_path / "stderr")
    with pytest.raises(RpcError) as info:
        rpc.start(0.5)
    cause = {"malformed": "bad rpc line", "timeout": "timed out", "exit": "code"}[kind]
    assert cause in str(info.value)
    assert "current failure" in str(info.value)
    assert len(str(info.value).encode()) < 17000
    assert rpc.close() is not None


def test_a_credential_split_by_the_excerpt_boundary_is_not_exposed(tmp_path):
    peer = (
        "import sys; sys.stderr.write('x' * 16340 + ' Authorization: Bearer ' + 'a' * 40 + '\\n');"
        " sys.exit(4)"
    )
    rpc = RpcClient([sys.executable, "-c", peer], tmp_path, stderr_path=tmp_path / "stderr")
    with pytest.raises(RpcError) as info:
        rpc.start(5)
    assert "a" * 8 not in str(info.value)


def ended(status: str, message: str) -> dict:
    return {"agentInvoked": True, "status": status, "error": {"message": message}}


def limit_of(message: str, status: str = "error"):
    with pytest.raises(RpcError) as info:
        _accept_result(ended(status, message))
    return info.value


@pytest.mark.parametrize(
    ("message", "delay"),
    [
        ('429 {"error":"rate_limit_error"} retry-after-ms=15707000', 15707.0),
        ("429 slow down retry-after-ms=0", 0.0),
        ("429 too many requests, Retry-After: 120", 120.0),
        ("429 retry-after=0", 0.0),
        # several authoritative hints: the latest deadline wins
        ("429 retry-after-ms=5000 Retry-After: 90 retry-after-ms=1000", 90.0),
        ("429 Retry-After: 2678400 retry-after-ms=1000", 2678400.0),
        (f"429 retry-after-ms={10**15 - 1}", (10**15 - 1) / 1000),
        ("429 Retry-After: 99999999999", 99999999999.0),
    ],
)
def test_a_terminal_429_carries_its_deadline(message, delay):
    before = time.time()
    error = limit_of(message)
    after = time.time()
    assert error.rate_limit is not None
    assert before + delay <= error.rate_limit.retry_at <= after + delay
    assert str(error) == f"prompt ended error: {message}"


def test_a_retry_after_date_is_an_absolute_deadline():
    when = datetime.now(timezone.utc) + timedelta(hours=2)
    error = limit_of(f"429 Retry-After: {format_datetime(when, usegmt=True)}")
    assert error.rate_limit.retry_at == pytest.approx(when.timestamp(), abs=1)
    past = limit_of("429 Retry-After: Wed, 21 Oct 2015 07:28:00 GMT")
    assert past.rate_limit.retry_at < time.time()  # due at the next scan, never a busy loop


@pytest.mark.parametrize(
    "date",
    [
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ],
)
def test_all_http_date_forms_are_absolute_utc_deadlines(monkeypatch, date):
    try:
        with monkeypatch.context() as scoped:
            scoped.setenv("TZ", "EST5")
            time.tzset()
            error = limit_of(f"429 Retry-After: {date}")
            assert error.rate_limit.retry_at == 784111777.0
    finally:
        time.tzset()


def test_a_pre_epoch_http_date_is_an_already_due_nonnegative_deadline():
    error = limit_of("429 Retry-After: Wed, 31 Dec 1969 23:59:59 GMT")
    assert error.rate_limit.retry_at == 0.0


@pytest.mark.parametrize(
    "message",
    [
        "429 usage limit reached",
        "429 retry-after-ms=soon",
        "429 Retry-After: -5",
        "429 Retry-After: 1.5",
        "429 Retry-After: tomorrow",
        "429 retry-after-ms=1e3",
        "429 Retry-After: 120seconds",
        "429 retry-after-ms=1.",
        "429 Retry-After: 1.0",
        "429 Retry-After: " + "9" * 5000,
    ],
)
def test_a_terminal_429_without_a_usable_hint_has_no_deadline(message):
    error = limit_of(message)
    assert error.rate_limit is not None and error.rate_limit.retry_at is None


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("error", "Unhandled API in mapOptionsForApi: web-search"),
        ("error", "internal error, upstream said 429 earlier retry-after-ms=1000"),
        ("aborted", "429 retry-after-ms=1000"),
        ("error", ""),
    ],
)
def test_only_a_terminal_429_is_a_rate_limit(status, message):
    assert limit_of(message, status).rate_limit is None


def test_a_rate_limit_diagnostic_hides_credentials():
    error = limit_of("429 Authorization: Bearer abcdef0123456789 retry-after-ms=1000")
    assert "abcdef0123456789" not in str(error) and "retry-after-ms=1000" in str(error)

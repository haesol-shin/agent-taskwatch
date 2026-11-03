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

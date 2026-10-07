"""真实有限流协议与SDK边界double，不连接生产S3。"""

import asyncio
import threading
import time

import pytest
from botocore.exceptions import ClientError

from artifact_store.transport import BlobObjectNotFoundError, BlobReadLimitExceeded
from connectors.object_store.config import S3ObjectStoreSettings
from shared.errors import TransientError
from shared.schemas.evidence_read import ObjectReadLimits

KEY = "raw/tn_01KZXT00000000000000000001/art_01KZXT00000000000000000001"


class FiniteBody:
    def __init__(self, data=b"abcd", delay=0):
        self.data = data
        self.delay = delay
        self.closed = False
        self.requests = []
        self.started = threading.Event()

    def read(self, n):
        assert type(n) is int and n > 0
        self.requests.append(n)
        self.started.set()
        time.sleep(self.delay)
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def close(self):
        self.closed = True


class Secrets:
    def __init__(self):
        self.calls = 0

    def resolve(self, ref):
        self.calls += 1
        return "controlled-only"


class Client:
    def __init__(self, body):
        self.body = body
        self.closed = False
        self.error = None

    def get_object(self, **kwargs):
        if self.error:
            raise self.error
        return {"Body": self.body, "ContentLength": 1}

    def close(self):
        self.closed = True


def case(monkeypatch, body=None, **overrides):
    from connectors.object_store import bounded

    limits = ObjectReadLimits(
        **{
            "connect_timeout_ms": 1000,
            "read_timeout_ms": 1000,
            "total_timeout_ms": 5000,
            "chunk_bytes": 65536,
            "maximum_attempts": 1,
            **overrides,
        }
    )
    settings = S3ObjectStoreSettings(
        True,
        "http://localhost:9000",
        "test-bucket",
        "TEST_ACCESS",
        "TEST_SECRET",
        "us-east-1",
        2097152,
        2097152,
    )
    secrets = Secrets()
    client = Client(body or FiniteBody())
    configs = []

    def sdk(*args, **kwargs):
        configs.append(kwargs["config"])
        return client

    monkeypatch.setattr(bounded.boto3, "client", sdk)
    reader = bounded.S3BoundedObjectBlobTransport(settings, secrets, limits=limits)
    return reader, secrets, client, configs


async def test_constructor_is_lazy_and_limits_include_first_attempt(monkeypatch):
    reader, secrets, client, configs = case(monkeypatch)
    assert secrets.calls == 0 and configs == []
    assert await reader.get_bounded(KEY, maximum_bytes=4) == b"abcd"
    assert client.closed and client.body.closed
    assert sum(client.body.requests) <= 6
    assert configs[0].retries["total_max_attempts"] == 1
    assert configs[0].connect_timeout == configs[0].read_timeout == 1


async def test_lying_size_stops_at_single_sentinel(monkeypatch):
    body = FiniteBody(b"a" * 100)
    reader, _, client, _ = case(monkeypatch, body, chunk_bytes=2)
    with pytest.raises(BlobReadLimitExceeded):
        await reader.get_bounded(KEY, maximum_bytes=4)
    assert body.requests == [2, 2, 1]
    assert len(body.data) == 95 and body.closed and client.closed


@pytest.mark.parametrize(
    "code,expected",
    [
        ("NoSuchKey", BlobObjectNotFoundError),
        ("AccessDenied", TransientError),
        ("SlowDown", TransientError),
    ],
)
async def test_sdk_errors_remain_fixed(monkeypatch, code, expected):
    reader, _, client, _ = case(monkeypatch)
    client.error = ClientError(
        {"Error": {"Code": code, "Message": "do-not-leak"}}, "GetObject"
    )
    with pytest.raises(expected) as caught:
        await reader.get_bounded(KEY, maximum_bytes=4)
    assert "do-not-leak" not in str(caught.value) and client.closed


async def test_deadline_checks_after_blocking_read(monkeypatch):
    reader, _, client, _ = case(
        monkeypatch, FiniteBody(delay=0.04), total_timeout_ms=10
    )
    with pytest.raises(TransientError):
        await reader.get_bounded(KEY, maximum_bytes=4)
    assert client.closed and client.body.closed


async def test_cancel_waits_for_thread_then_closes(monkeypatch):
    body = FiniteBody(delay=0.08)
    reader, _, client, _ = case(monkeypatch, body, chunk_bytes=1)
    task = asyncio.create_task(reader.get_bounded(KEY, maximum_bytes=4))
    await asyncio.to_thread(body.started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed and client.closed and len(body.requests) == 1


@pytest.mark.parametrize("failure_at", ["body", "client", "both"])
async def test_limit_error_survives_close_error(monkeypatch, failure_at):
    body = FiniteBody(b"abcdef")
    reader, _, client, _ = case(monkeypatch, body)

    def close_body():
        body.closed = True
        raise RuntimeError("controlled-close-failure")

    def close_client():
        client.closed = True
        raise RuntimeError("controlled-close-failure")

    if failure_at in {"body", "both"}:
        monkeypatch.setattr(body, "close", close_body)
    if failure_at in {"client", "both"}:
        monkeypatch.setattr(client, "close", close_client)
    with pytest.raises(BlobReadLimitExceeded):
        await reader.get_bounded(KEY, maximum_bytes=4)
    assert body.closed and client.closed

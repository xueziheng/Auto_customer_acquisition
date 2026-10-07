"""真实旧S3委托与受控SDK验证惰性边界，不访问外部对象存储。"""

import asyncio
import importlib
import io
import threading

import pytest
from botocore.exceptions import ClientError

from artifact_store.transport import BlobObjectNotFoundError
from connectors.object_store.config import S3ObjectStoreSettings
from shared.errors import TransientError, ValidationError

KEY = "raw/tn_01KZXT00000000000000000001/art_01KZXT00000000000000000001"


class Secrets:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def resolve(self, ref):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private resolver detail")
        return "controlled-test-value"


class Client:
    def __init__(self):
        self.calls = []
        self.fault = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def _call(self, operation):
        self.calls.append(operation)
        if self.fault == "missing":
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, operation)
        if self.fault == "sdk":
            raise RuntimeError("private SDK detail")
        if self.fault == "block":
            self.started.set()
            assert self.release.wait(2)
            self.completed.set()

    def put_object(self, **kwargs):
        self._call("put")

    def get_object(self, **kwargs):
        self._call("get")
        return {"Body": io.BytesIO(b"original bytes")}

    def delete_object(self, **kwargs):
        self._call("delete")


def case(monkeypatch):
    assert importlib.util.find_spec("connectors.object_store.deferred") is not None, (
        "缺少旧S3延迟装配边界"
    )
    module = importlib.import_module("connectors.object_store.deferred")
    from connectors.object_store import s3

    secrets, client, sdk_calls = Secrets(), Client(), []

    def sdk(*args, **kwargs):
        sdk_calls.append(True)
        return client

    monkeypatch.setattr(s3.boto3, "client", sdk)
    settings = S3ObjectStoreSettings(
        True,
        "http://localhost:9000",
        "test-bucket",
        "TEST_ACCESS",
        "TEST_SECRET",
        "us-east-1",
        1000,
        1000,
    )
    return (
        module.DeferredS3ObjectBlobTransport(settings, secrets),
        secrets,
        client,
        sdk_calls,
    )


async def test_constructor_is_inert_then_concurrent_first_calls_share_real_delegate(
    monkeypatch,
):
    transport, secrets, client, sdk_calls = case(monkeypatch)
    assert secrets.calls == 0 and sdk_calls == []
    assert repr(transport) == "DeferredS3ObjectBlobTransport()"
    results = await asyncio.gather(
        transport.put(KEY, b""),
        transport.get(KEY),
        transport.delete(KEY),
    )
    assert results == [None, b"original bytes", None]
    assert secrets.calls == 2 and len(sdk_calls) == 1
    assert sorted(client.calls) == ["delete", "get", "put"]


@pytest.mark.parametrize("operation", ["put", "get", "delete"])
@pytest.mark.parametrize("key", ["bad", KEY.lower(), None, 3])
async def test_invalid_key_never_initializes_delegate(monkeypatch, operation, key):
    transport, secrets, client, sdk_calls = case(monkeypatch)
    with pytest.raises(ValidationError, match="Artifact object key 无效"):
        if operation == "put":
            await transport.put(key, b"bytes")
        else:
            await getattr(transport, operation)(key)
    assert secrets.calls == 0 and sdk_calls == [] and client.calls == []


@pytest.mark.parametrize("content", [None, "bytes", bytearray(b"bytes"), 1])
async def test_invalid_content_never_initializes_delegate(monkeypatch, content):
    transport, secrets, client, sdk_calls = case(monkeypatch)
    with pytest.raises(ValidationError, match="Artifact 对象内容无效"):
        await transport.put(KEY, content)
    assert secrets.calls == 0 and sdk_calls == [] and client.calls == []


async def test_failed_initialization_is_sanitized_and_can_retry(monkeypatch):
    transport, secrets, client, sdk_calls = case(monkeypatch)
    secrets.fail = True
    with pytest.raises(TransientError, match="^Artifact 对象存储暂不可用$"):
        await transport.get(KEY)
    assert sdk_calls == [] and client.calls == []
    secrets.fail = False
    assert await transport.get(KEY) == b"original bytes"
    assert secrets.calls == 3 and len(sdk_calls) == 1


@pytest.mark.parametrize(
    "fault,error", [("missing", BlobObjectNotFoundError), ("sdk", TransientError)]
)
@pytest.mark.parametrize("operation", ["put", "get", "delete"])
async def test_original_delegate_errors_are_preserved(
    monkeypatch, fault, error, operation
):
    transport, secrets, client, sdk_calls = case(monkeypatch)
    client.fault = fault
    with pytest.raises(error) as raised:
        if operation == "put":
            await transport.put(KEY, b"bytes")
        else:
            await getattr(transport, operation)(KEY)
    assert "private" not in str(raised.value)
    assert secrets.calls == 2 and len(sdk_calls) == 1


async def test_cancellation_waits_for_original_sdk_thread_then_propagates(monkeypatch):
    transport, _, client, _ = case(monkeypatch)
    client.fault = "block"
    task = asyncio.create_task(transport.put(KEY, b"bytes"))
    try:
        assert await asyncio.to_thread(client.started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        client.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.completed.is_set()

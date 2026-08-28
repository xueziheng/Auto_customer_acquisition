"""专用QUOTE_PDF writer使用真实SDK适配代码与受控客户端，零live网络。"""

import asyncio
import threading
import time

import pytest

from artifact_store.transport import QuotePdfBlobTransportError
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.quote_pdf import S3QuotePdfObjectBlobTransport
from shared.schemas.generated_documents import QuotePdfWriteLimits
from tests.unit.test_quote_evidence_s3 import Secrets

KEY = "generated/tn_01KZXT00000000000000000001/art_01KZXT00000000000000000001"


class Client:
    def __init__(self, fault=None):
        self.calls = []
        self.closed = False
        self.fault = fault
        self.started = threading.Event()

    def put_object(self, **kwargs):
        self.calls.append("put")
        self.started.set()
        if self.fault == "slow":
            time.sleep(0.04)
        if self.fault == "write":
            raise RuntimeError("private SDK detail")

    def delete_object(self, **kwargs):
        self.calls.append("delete")

    def close(self):
        self.closed = True
        if self.fault == "close":
            raise RuntimeError("private close detail")


def writer_case(monkeypatch, *, fault=None, total=1000):
    from connectors.object_store import quote_pdf

    secrets, client, configs = Secrets(), Client(fault), []

    def sdk(*args, **kwargs):
        configs.append(kwargs["config"])
        return client

    monkeypatch.setattr(quote_pdf.boto3, "client", sdk)
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
        S3QuotePdfObjectBlobTransport(
            settings,
            secrets,
            limits=QuotePdfWriteLimits(
                connect_timeout_ms=100,
                read_timeout_ms=100,
                total_timeout_ms=total,
                maximum_attempts=1,
            ),
        ),
        secrets,
        client,
        configs,
    )


async def test_writer_is_lazy_single_operation_and_get_never_touches_sdk(monkeypatch):
    writer, secrets, client, configs = writer_case(monkeypatch)
    assert secrets.calls == 0 and not configs
    with pytest.raises(QuotePdfBlobTransportError) as error:
        await writer.get(KEY)
    assert error.value.code == "read_unsupported" and secrets.calls == 0
    await writer.put(KEY, b"pdf")
    assert client.calls == ["put"] and client.closed
    assert configs[0].retries["total_max_attempts"] == 1
    await writer.delete(KEY)
    assert client.calls == ["put", "delete"]


@pytest.mark.parametrize(
    "key,content",
    [
        (KEY.replace("generated", "raw"), b"pdf"),
        ("bad", b"pdf"),
        (KEY, bytearray(b"pdf")),
        (KEY, b""),
    ],
)
async def test_invalid_writer_input_has_zero_secret_or_sdk_io(
    monkeypatch, key, content
):
    writer, secrets, _client, configs = writer_case(monkeypatch)
    with pytest.raises(QuotePdfBlobTransportError) as error:
        await writer.put(key, content)
    assert error.value.code == "invalid_input" and secrets.calls == 0 and not configs


@pytest.mark.parametrize("fault", ["write", "close", "slow"])
async def test_after_write_errors_are_unknown_not_safe_retry(monkeypatch, fault):
    writer, _, client, _ = writer_case(
        monkeypatch, fault=fault, total=10 if fault == "slow" else 1000
    )
    with pytest.raises(QuotePdfBlobTransportError) as error:
        await writer.put(KEY, b"pdf")
    assert (
        error.value.code == "outcome_unknown"
        and client.closed
        and client.calls == ["put"]
    )
    assert "private" not in str(error.value)


async def test_cancel_waits_for_sdk_and_close_without_deleting(monkeypatch):
    writer, _, client, _ = writer_case(monkeypatch, fault="slow")
    task = asyncio.create_task(writer.put(KEY, b"pdf"))
    async with asyncio.timeout(1):
        while not client.started.is_set():
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and client.calls == ["put"]

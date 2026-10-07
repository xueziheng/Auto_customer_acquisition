"""API 自有惰性客户端：未调用不初始化，已调用退出关闭且可重试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from connectors.openai import OpenAIJsonModelClient
from infra.secrets import EnvironmentSecretResolver
from tests.integration.test_api_runtime import _runtime_env


@pytest.mark.parametrize("opened", [False, True])
async def test_openai_owned_close_is_lazy_and_idempotent(opened: bool) -> None:
    calls: list[str] = []

    class SDK:
        def __init__(self) -> None:
            self.responses = self

        async def create(self, **kwargs: object) -> object:
            return SimpleNamespace(output_text="{}")

        async def close(self) -> None:
            calls.append("closed")

    def factory(key: str, timeout: float):
        calls.append("created")
        return SDK()

    client = OpenAIJsonModelClient(
        "MODEL_TEST_KEY",
        EnvironmentSecretResolver({"MODEL_TEST_KEY": "k" * 32}),
        client_factory=factory,
    )
    assert callable(getattr(client, "aclose", None)), "模型 client 缺少 owned cleanup"
    if opened:
        await client.complete_json(
            model="controlled", system_prompt="JSON", payload={}, max_output_tokens=128
        )
    await client.aclose()
    await client.aclose()
    assert calls == (["created", "closed"] if opened else [])


async def test_deferred_s3_closes_opened_sdk_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class SDK:
        def put_object(self, **kwargs: object) -> None:
            calls.append("put")

        def close(self) -> None:
            calls.append("close")

    def factory(*args: object, **kwargs: object):
        calls.append("create")
        return SDK()

    monkeypatch.setattr("connectors.object_store.s3.boto3.client", factory)
    env = _runtime_env("postgresql+asyncpg://127.0.0.1:1/test")
    transport = DeferredS3ObjectBlobTransport(
        S3ObjectStoreSettings.from_environ(env), EnvironmentSecretResolver(env)
    )
    assert callable(getattr(transport, "aclose", None)), (
        "旧上传 client 缺少 owned cleanup"
    )
    assert calls == []
    await transport.put(
        "raw/tn_01K00000000000000000000001/art_01K00000000000000000000002", b"test"
    )
    await transport.aclose()
    await transport.aclose()
    assert calls == ["create", "put", "close"]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("private-sdk-error"), asyncio.CancelledError()],
)
async def test_failed_model_close_keeps_resource_for_retry(
    failure: BaseException,
) -> None:
    from shared.errors import TransientError, ValidationError

    calls = 0

    class SDK:
        async def close(self) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise failure

    client = OpenAIJsonModelClient(
        "MODEL_TEST_KEY",
        EnvironmentSecretResolver({"MODEL_TEST_KEY": "k" * 32}),
        client_factory=lambda *args: SDK(),
    )
    await client._get_client()
    with pytest.raises(BaseException) as caught:
        await client.aclose()
    if isinstance(failure, Exception):
        assert isinstance(caught.value, TransientError)
        assert "private-sdk-error" not in str(caught.value)
    else:
        assert caught.value is failure
    with pytest.raises(ValidationError, match="已关闭"):
        await client._get_client()
    await client.aclose()
    await client.aclose()
    assert calls == 2


def test_api_constructor_failure_has_no_open_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from sqlalchemy.ext.asyncio import AsyncEngine

    from apps.api import runtime
    from apps.api.runtime_config import Phase1RuntimeSettings
    from connectors.evidence_text.client import LinuxEvidenceTextParser
    from tests.quotation_runtime_fixtures import quotation_settings_values

    env = _runtime_env("postgresql+asyncpg://127.0.0.1:1/test")
    env["TRADEOS_QUOTATION_SETTINGS_JSON"] = json.dumps(quotation_settings_values())

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("构造阶段不得启动 DB/SDK/parser IO")

    monkeypatch.setattr(AsyncEngine, "connect", forbidden)
    monkeypatch.setattr(OpenAIJsonModelClient, "_default_client", forbidden)
    monkeypatch.setattr("connectors.object_store.s3.boto3.client", forbidden)
    monkeypatch.setattr(LinuxEvidenceTextParser, "probe", forbidden)
    build = runtime.build_phase1_dependencies

    def failing_build(*args: object, **kwargs: object):
        build(*args, **kwargs)
        raise RuntimeError("assembly-private-marker")

    monkeypatch.setattr(runtime, "build_phase1_dependencies", failing_build)
    with pytest.raises(runtime.RuntimeStartupError) as caught:
        runtime.create_runtime_app_from_settings(
            Phase1RuntimeSettings.from_environ(env),
            secret_resolver=EnvironmentSecretResolver(env),
            object_store_settings=S3ObjectStoreSettings.from_environ(env),
        )
    assert "assembly-private-marker" not in str(caught.value)

"""原 Gateway 与真实预算仓储，只有最外侧模型 SDK 使用合成响应。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from infra.db.model_usage import SqlModelUsageRepository
from infra.db.reply_retry_budget import SqlReplyRetryBudget
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import ModelInvocationRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.model_invocation import ModelGenerationError, ModelRequest
from tests.integration.test_model_usage import limits
from tests.integration.test_qq_trial_model import FakeSDK, FixedBrokerProvider
from tests.integration.test_reply_retry import seed
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.qq_trial_model import TrialModelPorts
from tool_gateway.reply_retry_model import ReplyRetryModelGenerator


@pytest.mark.parametrize("mode", ["ok", "unknown"])
async def test_recovery_uses_original_gateway_and_cannot_dispatch_twice(
    integration_engine, mode
):
    sessions, tenant, run, message, actor, config, repo = await seed(integration_engine)
    await repo.request(tenant, message, actor, "retry-model-key")
    identity = await repo.retry_identity(tenant, message, config.version)

    class Authority:
        async def check(self, current):
            await repo.authorize_identity(current)

    sdk = FakeSDK(mode)
    ports = TrialModelPorts(
        Authority(),
        SqlModelUsageRepository(sessions),
        lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        HmacFingerprintProvider("v1", b"k" * 32),
        limits(max_input_bytes=32768, max_output_tokens=512, timeout_seconds=60),
        "retry-test",
        timedelta(seconds=90),
        "deepseek",
    )
    generator = ReplyRetryModelGenerator(
        identity,
        "test-model",
        SqlReplyRetryBudget(repo, tenant, message),
        ports,
        lambda b, i: FixedBrokerProvider(b, sdk),
    )
    request = ModelRequest(
        model="test-model",
        system_prompt="Return JSON",
        payload={"test": True},
        max_output_tokens=512,
    )
    if mode == "ok":
        response = await generator.generate(identity, request)
        assert response.usage.output_tokens == 10
    else:
        with pytest.raises(ModelGenerationError):
            await generator.generate(identity, request)
    count = len(sdk.requests)
    with pytest.raises(ModelGenerationError):
        await generator.generate(identity, request)
    assert len(sdk.requests) == count
    async with sessions() as db:
        row = await db.scalar(
            select(ReplyRetryRow).where(
                ReplyRetryRow.tenant_id == tenant, ReplyRetryRow.message_id == message
            )
        )
        assert row.classifications == 1 and row.provider_requests == count
        calls = (
            await db.scalars(
                select(ModelInvocationRow)
                .where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                )
                .order_by(ModelInvocationRow.sequence)
            )
        ).all()
        assert (
            len(calls) == 2
            and calls[0].state == "rejected"
            and calls[0].dispatched_at is None
        )
        assert calls[1].sequence == 1 and calls[1].state == (
            "succeeded" if mode == "ok" else "unknown"
        )
